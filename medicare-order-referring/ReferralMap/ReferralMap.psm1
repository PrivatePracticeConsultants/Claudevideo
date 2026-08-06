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
    # data.cms.gov open-data API base (keyless); overridable for tests.
    # Serves the Medicare Monthly Enrollment (county market size + Medicare
    # Advantage share) and Physician & Other Practitioners (real billed
    # therapy services) datasets.
    CmsApiBase = if ($env:RM_CMS_API_BASE) { $env:RM_CMS_API_BASE }
                 else { 'https://data.cms.gov/data-api/v1/dataset' }
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

# Taxonomy codes that define "outpatient rehab clinic" for this tool: the
# scope is outpatient PT, OT, and speech therapy. Audited against the full
# NUCC taxonomy table (v25.1, nucc.org).
# COUPLING WARNING: if you add a code here, you MUST also make sure at least
# one entry in $script:RmSearchTerms below phrase-matches that taxonomy's NPPES
# description, or providers with the new code will silently never be found —
# NPPES only supports description phrase search, and e.g. 'Physical Therapist'
# and 'Physical Therapy' are DIFFERENT phrases (this exact miss happened once:
# 4 vs 45 providers found in one ZIP).
# Clinic codes stay EXACT-match: the 261QR04 stem also carries Cardiac
# Facilities (261QR0404X) and Substance Use Disorder (261QR0405X) rehab,
# which are OUT of scope. 261QH0700X is NUCC's only speech-clinic code
# (outpatient speech practices enumerate under it); it can also cover
# hearing/audiology clinics — the honest label makes that visible.
$script:RmClinicTaxonomies = @{
    '261QP2000X' = 'Clinic/Center: Physical Therapy'
    '261QR0400X' = 'Clinic/Center: Rehabilitation'
    '261QR0401X' = 'Clinic/Center: Rehabilitation, CORF (outpatient)'
    '261QH0700X' = 'Clinic/Center: Hearing and Speech'
}
# Individual PT/OT/SLP are matched by CODE PREFIX, so board-certified
# subspecialty enumerations (Orthopedic PT 2251X0800X, Hand OT 225XH1200X,
# Pediatric PT 2251P0200X, ...) are included — a specialist who lists ONLY
# the subspecialty code is still a therapist. The prefixes split cleanly in
# NUCC: PT assistants are 2252*, OT assistants 224Z*, speech-language
# ASSISTANTS 2355* — all on different stems, all excluded. Physiatrists
# (Physical Medicine & Rehabilitation, 2081*) are physicians — referral
# SOURCES, not competitors — and stay excluded too.
$script:RmIndividualTaxonomyPrefixes = @{
    '2251' = 'Physical Therapist'
    '225X' = 'Occupational Therapist'
    '235Z' = 'Speech-Language Pathologist'
}
# NPPES taxonomy_description search terms used to sweep a ZIP (results are then
# filtered to the exact codes above). NPPES phrase-matches descriptions, so
# "Physical Therapist" (individuals) and "Physical Therapy" (clinics/centers)
# are DIFFERENT searches — both are needed.
# NUCC 261Q00000X is the GENERIC "Clinic/Center" code - it says a provider is
# an ambulatory clinic and nothing about what it treats. Chains use it heavily:
# 277 of Athletico's 426 clinic NPIs carry it as PRIMARY with their real PT
# code in a spare slot, and ranking on primary alone therefore dropped 65% of
# the largest chain in the market out of every competitor table. A generic
# clinic that ALSO carries a therapy taxonomy is a therapy clinic. Measured
# nationally, this readmits 910 providers of the 27,742 holding this primary,
# only 2.5% of them hospital-named - it does not reopen the door to hospitals,
# which is what ranking-on-primary exists to prevent.
# A Fable-pass volume audit found the same pattern behind two more codes:
# in St Louis, Apex Physical Therapy (16,872 patients, would rank #3) sat
# on 174400000X "Specialist" - a legacy code NUCC itself calls non-specific
# - and EmpowerMe Rehabilitation Missouri (15,605, would rank #4) on
# 261QM1300X "Multi-Specialty Clinic", which is exactly how a PT+OT+SLP
# company reads to a form. Nationally the three codes readmit 910 + 328 +
# 582 providers, 0.3-2.6% of them hospital-named; the legacy 193x group
# codes were measured too and recover zero, so they are not here.
$script:RmGenericPrimaryTaxonomies = @{
    '261Q00000X' = 'Clinic/Center (non-specific)'
    '261QM1300X' = 'Clinic/Center: Multi-Specialty'
    '174400000X' = 'Specialist (legacy, non-specific)'
}

$script:RmSearchTerms = @('Physical Therapist', 'Physical Therapy',
                          'Occupational Therapist', 'Speech-Language Pathologist',
                          'Rehabilitation', 'Hearing and Speech')

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
        [ValidateRange(0, 100000)][int]$EnrichCap,
        [string]$CmsApiBase
    )
    if ($DataDir)  { $script:RmConfig.DataDir = $DataDir }
    if ($Year)     { $script:RmConfig.Year = $Year }
    if ($Interval) { $script:RmConfig.Interval = $Interval }
    if ($PSBoundParameters.ContainsKey('EnrichCap')) { $script:RmConfig.EnrichCap = $EnrichCap }
    if ($CmsApiBase) { $script:RmConfig.CmsApiBase = $CmsApiBase }
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
using System.Text.RegularExpressions;

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
        // A count that does not parse is a MALFORMED row, not a zero - a
        // silent 0 would deflate volumes invisibly (e.g. a future release
        // that stars suppressed pairs). Throwing routes the row into the
        // same badLines budget as a wrong field count.
        int v;
        double d;
        if (format == FormatHopTeaming)
        {
            if (!int.TryParse(f[2].Trim(), out v)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric patient_count ('" + f[2].Trim() + "').");
            e.BeneCount = v;
            if (!int.TryParse(f[3].Trim(), out v)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric transaction_count ('" + f[3].Trim() + "').");
            e.PairCount = v;
            e.SameDayCount = 0;                                             // not in this file
            // InvariantCulture: the file uses '.' decimals; a machine with a
            // ','-decimal locale must not misread 52.3 as 523.
            if (!double.TryParse(f[4].Trim(), System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out d)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric average_day_wait ('" + f[4].Trim() + "').");
            e.AvgDayWait = d;
        }
        else
        {
            if (!int.TryParse(f[2].Trim(), out v)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric pair count ('" + f[2].Trim() + "').");
            e.PairCount = v;
            if (!int.TryParse(f[3].Trim(), out v)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric patient count ('" + f[3].Trim() + "').");
            e.BeneCount = v;
            if (!int.TryParse(f[4].Trim(), out v)) throw new InvalidDataException(
                "Line " + lineNo + " of '" + path + "' has a non-numeric same-day count ('" + f[4].Trim() + "').");
            e.SameDayCount = v;
            e.AvgDayWait = 0.0;
        }
        return e;
    }

    // Streams the dataset file and returns rows matching a set of NPIs on one
    // column. matchColumn 1 = the SECOND provider in the pair (inbound: the
    // recipient); 0 = the FIRST (outbound: the initiator). Throws with a
    // plain message on a malformed file rather than returning wrong numbers.
    // Counts commas without allocating - the whole-file malformed check
    // must survive the fast path below.
    private static int CommaCount(string line)
    {
        int c = 0;
        for (int i = 0; i < line.Length; i++) if (line[i] == ',') c++;
        return c;
    }

    // Extracts one comma-separated field as a trimmed string, allocating
    // only that field. Returns null when the field does not exist.
    private static string FieldAt(string line, int index)
    {
        int start = 0;
        for (int k = 0; k < index; k++)
        {
            start = line.IndexOf(',', start);
            if (start < 0) return null;
            start++;
        }
        int end = line.IndexOf(',', start);
        if (end < 0) end = line.Length;
        string v = line.Substring(start, end - start);
        return (v.Length > 0 && (v[0] == ' ' || v[v.Length - 1] == ' ')) ? v.Trim() : v;
    }

    private static List<RmEdge> Scan(string path, HashSet<string> match, int matchColumn, int format)
    {
        // MATCH FIRST, PARSE ON HIT. The old path Split() every one of the
        // ~210M rows into 6 strings before the match check discarded
        // 99.99% of them - measured at 188s per 8 GB pass. Here only the
        // key field is materialized per row; the full parse (and its
        // strict validation) runs on matches and on the first 5 lines, and
        // a no-allocation comma count keeps the whole-file malformed
        // guard exact.
        List<RmEdge> edges = new List<RmEdge>();
        long lineNo = 0;
        long badLines = 0;
        int wantCommas = ((format == FormatHopTeaming) ? 6 : 5) - 1;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                if (lineNo <= 5)
                {
                    RmEdge e0;
                    try { e0 = ParseLine(line, format, path, lineNo); }
                    catch (InvalidDataException) { throw; }
                    if (e0 == null) continue;   // header row
                    string k0 = (matchColumn == 1) ? e0.TargetNpi : e0.SourceNpi;
                    if (match.Contains(k0)) edges.Add(e0);
                    continue;
                }
                if (CommaCount(line) != wantCommas) { badLines++; continue; }
                string key = FieldAt(line, matchColumn);
                if (key == null || !match.Contains(key)) continue;
                RmEdge e;
                try { e = ParseLine(line, format, path, lineNo); }
                catch (InvalidDataException) { badLines++; continue; }
                if (e != null) edges.Add(e);
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

    // ONE pass serving the whole source analysis: rows where the practice is
    // either endpoint (inbound analysis AND outbound destinations) plus rows
    // where a COMPETITOR received the patient (ranking and market capture).
    // The analysis used to make three separate passes over the same 8 GB for
    // these; the caller routes each row by checking the same two sets.
    public static List<RmEdge> ScanCombined(string path, HashSet<string> members,
        HashSet<string> peers, int format)
    {
        List<RmEdge> edges = new List<RmEdge>();
        long lineNo = 0, badLines = 0;
        int wantCommas = ((format == FormatHopTeaming) ? 6 : 5) - 1;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                if (lineNo <= 5)
                {
                    RmEdge e0;
                    try { e0 = ParseLine(line, format, path, lineNo); }
                    catch (InvalidDataException) { throw; }
                    if (e0 == null) continue;
                    if (members.Contains(e0.SourceNpi) || members.Contains(e0.TargetNpi)
                        || (peers != null && peers.Contains(e0.TargetNpi))) edges.Add(e0);
                    continue;
                }
                if (CommaCount(line) != wantCommas) { badLines++; continue; }
                string tgt = FieldAt(line, 1);
                bool hit = (tgt != null) && (members.Contains(tgt) || (peers != null && peers.Contains(tgt)));
                if (!hit)
                {
                    string src = FieldAt(line, 0);
                    hit = (src != null) && members.Contains(src);
                }
                if (!hit) continue;
                RmEdge e;
                try { e = ParseLine(line, format, path, lineNo); }
                catch (InvalidDataException) { badLines++; continue; }
                if (e != null) edges.Add(e);
            }
        }
        if (lineNo == 0) throw new InvalidDataException("The file '" + path + "' is empty.");
        if (badLines > lineNo / 100)
            throw new InvalidDataException("The file '" + path + "' had too many malformed lines.");
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
        int wantCommas = ((format == FormatHopTeaming) ? 6 : 5) - 1;
        using (StreamReader reader = new StreamReader(path, Encoding.ASCII, false, 1 << 20))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                lineNo++;
                if (line.Length == 0) continue;
                if (lineNo <= 5)
                {
                    RmEdge e0;
                    try { e0 = ParseLine(line, format, path, lineNo); }
                    catch (InvalidDataException) { throw; }
                    if (e0 == null) continue;
                    if (set.Contains(e0.SourceNpi) || set.Contains(e0.TargetNpi)) edges.Add(e0);
                    continue;
                }
                if (CommaCount(line) != wantCommas) { badLines++; continue; }
                string src = FieldAt(line, 0);
                string tgt = FieldAt(line, 1);
                if ((src == null || !set.Contains(src)) && (tgt == null || !set.Contains(tgt))) continue;
                RmEdge e;
                try { e = ParseLine(line, format, path, lineNo); }
                catch (InvalidDataException) { badLines++; continue; }
                if (e != null) edges.Add(e);
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

    // ---- Roster indexes (NPPES bulk file / Care Compare DAC) -------------
    // Both rosters are large quoted CSVs; both are reduced ONCE at import to
    // a compact pipe-delimited index that later runs SCAN (streaming, like
    // the dataset engine) instead of loading into memory.

    private static List<string> SplitCsv(string line)
    {
        List<string> fields = new List<string>();
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
            else if (c == '"') q = true;
            else if (c == ',') { fields.Add(cur.ToString()); cur.Length = 0; }
            else cur.Append(c);
        }
        fields.Add(cur.ToString());
        return fields;
    }

    private static string Clean(string s)
    {
        return s == null ? "" : s.Replace("|", "/").Trim();
    }

    // Projects named columns of a huge quoted CSV into a compact pipe file.
    // Column indexes are resolved from the HEADER by name, so a layout shift
    // in a future release fails loudly instead of silently mis-mapping.
    public static long BuildRosterIndex(string src, string dest, string[] wantedColumns)
    {
        using (StreamReader r = new StreamReader(src, Encoding.UTF8, true, 1 << 20))
        {
            return BuildRosterIndexCore(r, dest, wantedColumns);
        }
    }

    // Streams the roster straight out of a zip ENTRY STREAM the caller
    // opens (PowerShell owns the zip handling) - the NPPES inner CSV is
    // 11+ GB and must never be extracted to disk first.
    public static long BuildRosterIndexFromStream(Stream src, string dest, string[] wantedColumns)
    {
        StreamReader r = new StreamReader(src, Encoding.UTF8, true, 1 << 20);
        return BuildRosterIndexCore(r, dest, wantedColumns);
    }

    private static long BuildRosterIndexCore(StreamReader r, string dest, string[] wantedColumns)
    {
        long rows = 0;
        using (r)
        using (StreamWriter w = new StreamWriter(dest, false, new UTF8Encoding(false), 1 << 20))
        {
            string header = r.ReadLine();
            if (header == null) throw new Exception("empty file");
            List<string> cols = SplitCsv(header);
            int[] idx = new int[wantedColumns.Length];
            for (int i = 0; i < wantedColumns.Length; i++)
            {
                idx[i] = -1;
                for (int j = 0; j < cols.Count; j++)
                    if (string.Equals(cols[j].Trim(), wantedColumns[i], StringComparison.OrdinalIgnoreCase)) { idx[i] = j; break; }
                if (idx[i] < 0) throw new Exception("column not found: " + wantedColumns[i]);
            }
            string line;
            StringBuilder outLine = new StringBuilder();
            while ((line = r.ReadLine()) != null)
            {
                List<string> f = SplitCsv(line);
                outLine.Length = 0;
                for (int i = 0; i < idx.Length; i++)
                {
                    if (i > 0) outLine.Append('|');
                    outLine.Append(idx[i] < f.Count ? Clean(f[idx[i]]) : "");
                }
                w.WriteLine(outLine.ToString());
                rows++;
            }
        }
        return rows;
    }

    // Streams a compact index and returns every line whose FIELD matches one
    // of the wanted values. One pass, memory bounded by the match count.
    public static List<string> ScanRosterIndex(string path, HashSet<string> wanted, int fieldIndex)
    {
        List<string> hits = new List<string>();
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                int start = 0; int fi = 0; string val = null;
                while (fi <= fieldIndex)
                {
                    int p = line.IndexOf('|', start);
                    if (fi == fieldIndex) { val = p < 0 ? line.Substring(start) : line.Substring(start, p - start); break; }
                    if (p < 0) break;
                    start = p + 1; fi++;
                }
                if (val != null && wanted.Contains(val)) hits.Add(line);
            }
        }
        return hits;
    }

    // First-field membership scan over any delimited file (CSV or PSV):
    // returns the full lines whose FIRST field is in the wanted set. Used to
    // check referral sources against the 2M-row Order & Referring roster
    // without a PowerShell-speed loop.
    public static List<string> MatchFirstField(string path, HashSet<string> wanted, char sep)
    {
        List<string> hits = new List<string>();
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                int p = line.IndexOf(sep);
                string first = p < 0 ? line : line.Substring(0, p);
                if (first.Length > 1 && first[0] == '"') first = first.Trim('"');
                if (wanted.Contains(first)) hits.Add(line);
            }
        }
        return hits;
    }

    // Pipe-field extractor for the PSV indexes (no quoting in those files).
    private static string PipeFieldAt(string line, int index)
    {
        int start = 0;
        for (int k = 0; k < index; k++)
        {
            start = line.IndexOf('|', start);
            if (start < 0) return null;
            start++;
        }
        int end = line.IndexOf('|', start);
        if (end < 0) end = line.Length;
        return line.Substring(start, end - start);
    }

    // ZIP-sweep over an index: returns the full lines whose postal field
    // starts in the wanted set (or matches a prefix). The PowerShell loop
    // this replaces Split() all 9.7M rows per map search.
    public static List<string> ScanIndexByZip(string path, int zipField, HashSet<string> zips, List<string> prefixes)
    {
        List<string> hits = new List<string>();
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                string postal = PipeFieldAt(line, zipField);
                if (postal == null || postal.Length < 5) continue;
                string z5 = postal.Substring(0, 5);
                bool hit = zips.Contains(z5);
                if (!hit && prefixes != null)
                {
                    for (int i = 0; i < prefixes.Count; i++)
                        if (z5.StartsWith(prefixes[i], StringComparison.Ordinal)) { hit = true; break; }
                }
                if (hit) hits.Add(line);
            }
        }
        return hits;
    }

    // Organization-name search over an index: needlePattern is the caller's
    // space-optional regex over the normalized key; compactNeedle catches
    // the spacing near-misses. entityField -1 = no entity filter (DAC).
    // Returns matching lines; near-miss NAMES accumulate into nearMiss.
    public static List<string> FindByOrgName(string path, int entityField, int nameField,
        string needlePattern, string compactNeedle, HashSet<string> nearMiss)
    {
        Regex rx = new Regex(needlePattern, RegexOptions.Compiled);
        List<string> hits = new List<string>();
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                if (entityField >= 0)
                {
                    string ent = PipeFieldAt(line, entityField);
                    if (ent != "2") continue;
                }
                string name = PipeFieldAt(line, nameField);
                if (string.IsNullOrEmpty(name)) continue;
                string key = OrgNameKey(name);
                if (key.Length == 0) continue;
                if (rx.IsMatch(key)) { hits.Add(line); continue; }
                if (nearMiss != null && key.Replace(" ", "").Contains(compactNeedle)) nearMiss.Add(name);
            }
        }
        return hits;
    }

    // Related-name grouping for a brand's lead word: distinct addresses per
    // normalized name whose key STARTS WITH the lead (not contains - 'ATI'
    // is a substring of REHABILITATION), excluding keys the main search
    // already matched. This was the last PowerShell loop over a
    // million-row index: measured live it crawled the 3.4M-row Care
    // Compare file at ~135 KB/s (about 40 minutes) because the name key
    // costs four regex operations per row in script; here it is seconds.
    // Returns "displayName\u0001addressCount" rows; the tiny result is
    // sorted by the caller.
    // Facility-name key -> distinct clinician count over the whole Care
    // Compare index. Powers the map's RosterSize column: how many clinicians
    // practice under that organization name (all its locations combined).
    public static Dictionary<string, int> ScanDacRosterCounts(string path, int npiField, int nameField)
    {
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        Dictionary<string, int> counts = new Dictionary<string, int>(StringComparer.Ordinal);
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                string npi = PipeFieldAt(line, npiField);
                string name = PipeFieldAt(line, nameField);
                if (string.IsNullOrEmpty(npi) || string.IsNullOrEmpty(name)) continue;
                string key = OrgNameKey(name);
                if (key.Length == 0) continue;
                if (!seen.Add(key + "\u0001" + npi)) continue;
                int c;
                counts[key] = counts.TryGetValue(key, out c) ? c + 1 : 1;
            }
        }
        return counts;
    }

    public static List<string> RelatedOrgAddressCounts(string path, int nameField, int addrField,
        int zipField, string leadWord, string excludeNeedle)
    {
        Dictionary<string, HashSet<string>> addrs = new Dictionary<string, HashSet<string>>();
        Dictionary<string, string> display = new Dictionary<string, string>();
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                string name = PipeFieldAt(line, nameField);
                if (string.IsNullOrEmpty(name)) continue;
                string key = OrgNameKey(name);
                if (key.Length == 0 || !key.StartsWith(leadWord, StringComparison.Ordinal)) continue;
                if (excludeNeedle.Length > 0 && key.Contains(excludeNeedle)) continue;
                string addr = PipeFieldAt(line, addrField);
                string zip = PipeFieldAt(line, zipField);
                if (zip != null && zip.Length > 5) zip = zip.Substring(0, 5);
                HashSet<string> set;
                if (!addrs.TryGetValue(key, out set))
                {
                    set = new HashSet<string>();
                    addrs[key] = set;
                    display[key] = name;
                }
                set.Add(addr + "|" + zip);
            }
        }
        List<string> outRows = new List<string>();
        foreach (KeyValuePair<string, HashSet<string>> kv in addrs)
            outRows.Add(display[kv.Key] + "\u0001" + kv.Value.Count);
        return outRows;
    }

    // Organization-name key. MUST stay byte-identical to Get-RmOrgNameKey in
    // the module - a regression test asserts the two agree, because a drift
    // would silently flag the wrong companies.
    static readonly HashSet<string> NoiseWords = new HashSet<string>(new string[] {
        "LLC","INC","INCORPORATED","PC","PA","PLLC","LLP","LLLP","LP","LTD","LIMITED",
        "PLC","SC","PSC","APC","CORP","CORPORATION","COMPANY","CO","PARTNERSHIP","THE","OF","AND"
    });
    public static string OrgNameKey(string name)
    {
        if (name == null) return "";
        StringBuilder cleaned = new StringBuilder(name.Length);
        foreach (char ch in name.ToUpperInvariant())
        {
            if ((ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9')) cleaned.Append(ch);
            else cleaned.Append(' ');
        }
        string[] raw = cleaned.ToString().Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        // "P.C." tokenizes to "P" "C"; glue runs of single letters back into
        // one word so the punctuated form matches the plain one.
        List<string> merged = new List<string>(raw.Length);
        for (int i = 0; i < raw.Length; )
        {
            if (raw[i].Length == 1 && raw[i][0] >= 'A' && raw[i][0] <= 'Z'
                && i + 1 < raw.Length && raw[i + 1].Length == 1
                && raw[i + 1][0] >= 'A' && raw[i + 1][0] <= 'Z')
            {
                StringBuilder run = new StringBuilder();
                while (i < raw.Length && raw[i].Length == 1 && raw[i][0] >= 'A' && raw[i][0] <= 'Z')
                {
                    run.Append(raw[i]); i++;
                }
                merged.Add(run.ToString());
            }
            else { merged.Add(raw[i]); i++; }
        }
        StringBuilder outp = new StringBuilder(name.Length);
        foreach (string w in merged)
        {
            if (w.Length == 0 || NoiseWords.Contains(w)) continue;
            if (outp.Length > 0) outp.Append(' ');
            outp.Append(w);
        }
        return outp.ToString();
    }

    // Stamped into the derived chain file. Bump it whenever OrgNameKey
    // changes, so a table built under the old rules is discarded instead of
    // being read with keys that no longer match.
    public const string ChainIndexVersion = "#RMCHAIN|2";

    // One pass over the NPPES index -> "nameKey|orgNpiCount|cityCount" for
    // every ORGANIZATION name. In PowerShell this loop cost 541 seconds on
    // the real 687 MB index, which is not something a user can wait through
    // inside a ZIP search; here it is seconds.
    public static long BuildChainIndex(string indexPath, string dest)
    {
        Dictionary<string, int> counts = new Dictionary<string, int>(1 << 20);
        Dictionary<string, HashSet<string>> cities = new Dictionary<string, HashSet<string>>(1 << 20);
        using (StreamReader r = new StreamReader(indexPath, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                string[] f = line.Split('|');
                if (f.Length < 10 || f[1] != "2" || f[2].Length == 0) continue;
                string k = OrgNameKey(f[2]);
                if (k.Length == 0) continue;
                int c;
                if (counts.TryGetValue(k, out c)) counts[k] = c + 1;
                else { counts[k] = 1; cities[k] = new HashSet<string>(); }
                cities[k].Add(f[5] + "|" + f[6]);
            }
        }
        long rows = 0;
        using (StreamWriter w = new StreamWriter(dest, false, new UTF8Encoding(false), 1 << 20))
        {
            w.Write(ChainIndexVersion); w.Write('\n');
            foreach (KeyValuePair<string, int> kv in counts)
            {
                w.Write(kv.Key); w.Write('|'); w.Write(kv.Value);
                w.Write('|'); w.Write(cities[kv.Key].Count); w.Write('\n');
                rows++;
            }
        }
        return rows;
    }

    // Loads that file into a dictionary in one go. Doing it line-by-line in
    // PowerShell over 1.35 million names is itself slow enough to notice.
    public static Dictionary<string, int[]> LoadChainIndex(string path)
    {
        Dictionary<string, int[]> d = new Dictionary<string, int[]>(1 << 21);
        using (StreamReader r = new StreamReader(path, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = r.ReadLine()) != null)
            {
                if (line.Length > 0 && line[0] == '#') continue;   // version stamp
                int p1 = line.LastIndexOf('|');
                if (p1 <= 0) continue;
                int p0 = line.LastIndexOf('|', p1 - 1);
                if (p0 <= 0) continue;
                int npis, cty;
                if (!int.TryParse(line.Substring(p0 + 1, p1 - p0 - 1), out npis)) continue;
                if (!int.TryParse(line.Substring(p1 + 1), out cty)) continue;
                d[line.Substring(0, p0)] = new int[] { npis, cty };
            }
        }
        return d;
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
    # *.part covers download partials (local-resource downloads); everything
    # else temp in this dir is *.tmp. Same mtime guard spares active writes.
    foreach ($pat in @('*.tmp', '*.part')) {
        Get-ChildItem -LiteralPath $d -Filter $pat -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
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
    # GUID temp: the GUI and a scheduled task can both be running; a shared
    # fixed temp name would let one clobber the other mid-write.
    $tmp = $p + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
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
    # No usable metadata. Before giving up, RECOVER from what is actually on
    # disk: if CareSet years are sitting there (meta deleted, corrupted, or
    # the folder was copied to a new machine), adopt the newest one instead
    # of telling the user they have no data. Cosmetic recovery only — it
    # never overwrites a valid meta.
    $hop = @(Get-ChildItem -LiteralPath $script:RmConfig.DataDir -Filter 'hop_teaming_*.csv' -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            if ($_.Name -match '^hop_teaming_(\d{4})\.csv$') {
                [pscustomobject]@{ Year = [int]$Matches[1]; Path = $_.FullName }
            }
        } | Sort-Object Year -Descending)
    if ($hop.Count -gt 0) {
        $pick = $hop[0]
        Write-Verbose "No dataset metadata; recovered $($pick.Path) from disk."
        return [pscustomobject]@{
            Ready  = $true
            Source = 'hop-teaming'
            Year   = $pick.Year
            Path   = $pick.Path
            Format = [RmEngine]::FormatHopTeaming
            Rows   = 0
            Label  = "DocGraph Hop Teaming $($pick.Year) (CareSet)"
        }
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
            # NPPES is LIVE registry data, so cached answers expire: 30 days
            # for a found provider, 7 for "deactivated or not found" (which
            # providers routinely recover from - re-activation, late
            # enumeration). Without a TTL, a provider who moved stayed at the
            # old address in every future report until someone deleted this
            # file by hand. Entries from caches that predate the stamp count
            # as expired and refresh once (one local-index scan, or the API).
            $now = [DateTime]::UtcNow
            foreach ($prop in $json.PSObject.Properties) {
                $v = $prop.Value
                $age = $null
                if ($null -ne $v.PSObject.Properties['FetchedAt']) {
                    try {
                        $t = [DateTime]::Parse([string]$v.FetchedAt,
                            [System.Globalization.CultureInfo]::InvariantCulture,
                            [System.Globalization.DateTimeStyles]::RoundtripKind)
                        $age = ($now - $t.ToUniversalTime()).TotalDays
                    } catch { }
                }
                $ttl = if ([string](Get-RmProp $v 'Name') -eq '(NPI deactivated or not found)') { 7 } else { 30 }
                if ($null -ne $age -and $age -le $ttl) { $cache[$prop.Name] = $v }
            }
        } catch {
            Write-Warning "NPPES cache was unreadable and will be rebuilt: $($_.Exception.Message)"
        }
    }
    $cache
}

function Write-RmNppesCache([hashtable]$Cache) {
    Initialize-RmDataDir | Out-Null
    $p = Get-RmNppesCachePath
    $tmp = $p + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $Cache | ConvertTo-Json -Depth 5 -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $p -Force
}

function Test-RmNpiShape([string]$Npi) { $Npi -match '^\d{10}$' }

# A multi-GB download or extraction that is GUARANTEED to die on a full disk
# should die NOW with the number the user needs, not hours in. The probe is
# advisory: on exotic paths (UNC shares) it silently skips rather than block.
function Assert-RmDiskSpace([string]$Dir, [long]$NeededBytes, [string]$What) {
    $free = $null; $root = ''
    try {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Dir))
        if ($root) { $free = (New-Object System.IO.DriveInfo($root)).AvailableFreeSpace }
    } catch { }
    if ($null -ne $free -and $free -lt $NeededBytes) {
        throw ("Not enough disk space for {0}: it needs about {1:N1} GB free on {2}, but only {3:N1} GB is available. Free up space and try again." -f `
            $What, ($NeededBytes / 1GB), $root, ($free / 1GB))
    }
}

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
    # One transient hiccup (dropped TLS handshake, brief Wi-Fi blip) must not
    # kill a long multi-ZIP sweep that is 20 minutes in: 3 attempts with a
    # short backoff before the friendly failure surfaces to the caller.
    $delays = @(0, 2, 5)
    $lastMsg = ''
    foreach ($delay in $delays) {
        if ($delay) { Start-Sleep -Seconds $delay }
        try {
            return Invoke-RestMethod -Uri $url -TimeoutSec 60 -ErrorAction Stop
        } catch {
            $lastMsg = $_.Exception.Message
            Write-Verbose "NPPES request failed (will retry): $lastMsg"
        }
    }
    throw ("The NPPES registry (npiregistry.cms.hhs.gov) could not be reached " +
           "after $($delays.Count) attempts. Check your internet connection. Details: $lastMsg")
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
    # ~356 MB zip + ~1.7 GB extracted + the promoted copy's headroom.
    Assert-RmDiskSpace $script:RmConfig.DataDir 4GB 'downloading and extracting the CMS shared-patient file'
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
                Assert-RmDiskSpace $script:RmConfig.DataDir ([long]($entry[0].Length + 512MB)) `
                    "extracting the $([math]::Round($entry[0].Length/1GB,1)) GB Hop Teaming file"
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
            $srcLen = (Get-Item -LiteralPath $csvSource).Length
            Assert-RmDiskSpace $script:RmConfig.DataDir ([long]($srcLen + 512MB)) `
                "copying the $([math]::Round($srcLen/1GB,1)) GB Hop Teaming file into the data folder"
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
        [ValidatePattern('^\d{3,5}\*?$')][string]$Zip,
        [ValidateCount(1, 2000)][string[]]$ZipList,   # exact 5-digit ZIPs (radius search)
        [switch]$OrganizationsOnly
    )
    if (-not $Zip -and -not $ZipList) { throw 'Provide -Zip or -ZipList.' }

    # Label for an in-scope code, $null when out of scope. Clinic codes are
    # exact (the 261QR04 stem also carries cardiac/substance-use rehab, out
    # of scope); individual PT/OT/SLP match by prefix so subspecialty
    # enumerations count. The label prefers NPPES's own description so a
    # subspecialty shows honestly (e.g. "Physical Therapist Orthopedic").
    $resolveTax = {
        param($code, $desc)
        if ($script:RmClinicTaxonomies.ContainsKey($code)) { return $script:RmClinicTaxonomies[$code] }
        if ($OrganizationsOnly -or $code.Length -lt 4) { return $null }
        $base = $script:RmIndividualTaxonomyPrefixes[$code.Substring(0, 4)]
        if (-not $base) { return $null }
        # NPPES descriptions for un-specialized codes carry a trailing
        # separator ("Speech-Language Pathologist, "); tidy it so grids and
        # client reports never show a dangling comma.
        $clean = if ($desc) { $desc.Trim().TrimEnd(',', ' ', '-').Trim() } else { '' }
        if ($clean) { $clean } else { $base }
    }

    # Local NPPES bulk index, when imported: ONE streaming pass finds every
    # provider in the requested ZIP(s). This is strictly more accurate than
    # the live API path — no 1,200-result ceiling, no phrase-search gaps,
    # no rate limits — and it works offline.
    $bulkIdx = Get-RmNppesIndexPath
    if (Test-Path -LiteralPath $bulkIdx) {
        $zipWanted = New-Object 'System.Collections.Generic.HashSet[string]'
        $prefixes = New-Object System.Collections.Generic.List[string]
        foreach ($z in @(if ($ZipList) { $ZipList } else { @($Zip) })) {
            $zz = [string]$z
            if ($zz -match '^\d{5}$') { [void]$zipWanted.Add($zz) } else { $prefixes.Add($zz.TrimEnd('*')) }
        }
        Write-Verbose "NPPES bulk index: sweeping $($zipWanted.Count) ZIP(s)$(if ($prefixes.Count) { " + $($prefixes.Count) prefix(es)" })..."
        $found2 = @{}
        # The ZIP filter runs in C# (the PowerShell per-line loop over 9.7M
        # rows was the tab's dominant cost); only the few thousand matched
        # lines are parsed here.
        $pfxList = New-Object 'System.Collections.Generic.List[string]'
        foreach ($p in $prefixes) { $pfxList.Add([string]$p) }
        foreach ($line in @([RmEngine]::ScanIndexByZip($bulkIdx, 7, $zipWanted, $pfxList))) {
            $f = $line.Split('|')
            if ($f.Count -lt 10) { continue }
            $z5 = $f[7].Substring(0, 5)
            # ANY taxonomy slot may carry the in-scope code (primary at 8,
            # secondaries from 10 on) — matching only the primary silently
            # dropped real therapy providers.
            $label = $null; $matchedCode = ''
            $taxEnd = [math]::Min($f.Count - 1, 23)   # codes live at 8, 10..23; switches at 24+
            foreach ($ti in @(8) + @(10..$taxEnd)) {
                if ($ti -ge $f.Count) { break }
                $code = $f[$ti]
                if (-not $code) { continue }
                $label = & $resolveTax $code ''
                if ($label) { $matchedCode = $code; break }
            }
            $nameRescue = ''
            if (-not $label) {
                # NAME RESCUE: an organization that registered NO therapy
                # taxonomy anywhere, but whose legal or DBA name literally
                # says it is a PT/OT/SLP practice. Measured nationwide: 469
                # such orgs carried 785,074 patients in 2022 (Reddy Care
                # Physical Therapy alone 89,005) - most hold the legacy
                # 'Specialist' code 174400000X, so the taxonomy sweep can
                # never see them. Facility names (hospital, nursing, home
                # health...) are refused by the strict name test.
                if ($f[1] -ne '2') { continue }
                if (Test-RmTherapyPracticeName $f[2]) {
                    $nameRescue = 'therapy-named practice (no therapy taxonomy registered)'
                } else {
                    $ont = Get-RmOtherNameTable
                    if ($ont.ContainsKey($f[0])) {
                        foreach ($dn in $ont[$f[0]]) {
                            if (Test-RmTherapyPracticeName $dn) {
                                $nameRescue = "therapy-named practice (DBA '$dn'; no therapy taxonomy registered)"
                                break
                            }
                        }
                    }
                }
                if (-not $nameRescue) { continue }
            }
            # PRIMARY = the slot whose Switch_N is 'Y' (slot ORDER is not
            # primacy - see the importer note). Older indexes without switch
            # fields fall back to slot 1.
            $primaryCode = Get-RmIndexPrimaryCode $f
            $primaryInScope = [bool](& $resolveTax $primaryCode '')
            if (-not $primaryInScope -and $script:RmGenericPrimaryTaxonomies.ContainsKey($primaryCode)) {
                # Generic "Clinic/Center" primary + a real therapy code in
                # another slot ($label proved one is present) = comparable.
                $primaryInScope = $true
            }
            if ($nameRescue) { $primaryInScope = $true }   # its name IS the evidence
            $isOrg = $f[1] -eq '2'
            $found2[$f[0]] = [pscustomobject]@{
                NPI = $f[0]
                Name = if ($isOrg) { $f[2] } else { ("$($f[4]) $($f[3])").Trim() }
                Type = if ($isOrg) { 'Organization' } else { 'Individual' }
                Taxonomy = if ($nameRescue) { Get-RmTaxonomyName (Get-RmIndexPrimaryCode $f) } else { Get-RmTaxonomyName $matchedCode }
                City = $f[5]; State = $f[6]; Zip = $z5
                Enumerated = if ($f[9] -match '^(\d{2})/(\d{2})/(\d{4})$') { "$($Matches[3])-$($Matches[1])-$($Matches[2])" } else { $f[9] }
                # TRUE when therapy is the provider's PRIMARY taxonomy. A
                # hospital that merely lists a therapy taxonomy in a spare
                # slot is a real provider but NOT a comparable therapy
                # practice — its inbound volume spans every service line.
                PrimaryInScope = $primaryInScope
                Presence = $nameRescue
            }
        }
        # ---- Secondary practice locations --------------------------------
        # A practice can TREAT in this area while its Medicare enrollment is
        # registered outside it - NPPES's secondary-location file is where
        # those clinics live (measured in St Louis 63101+30mi: 169 therapy
        # NPIs incl. AXES Physical Therapy with five in-ring clinics and
        # 19,287 patients, registered one town past the radius). Sweep it so
        # those practices appear instead of silently missing from the map.
        # Needs a locations index built by a current NPPES import; older
        # indexes carry no ZIPs and the sweep quietly contributes nothing.
        $locZipTbl = Get-RmSecondaryLocationZipTable
        if ($locZipTbl.Count -gt 0) {
            $secWant = New-Object 'System.Collections.Generic.HashSet[string]'
            $secZip = @{}
            foreach ($kv in $locZipTbl.GetEnumerator()) {
                if ($found2.ContainsKey($kv.Key)) { continue }
                foreach ($sz in ([string]$kv.Value).Split(';')) {
                    $hit = $zipWanted.Contains($sz)
                    if (-not $hit) {
                        foreach ($pf in $pfxList) { if ($sz.StartsWith($pf)) { $hit = $true; break } }
                    }
                    if ($hit) { [void]$secWant.Add($kv.Key); $secZip[$kv.Key] = $sz; break }
                }
            }
            if ($secWant.Count -gt 0) {
                foreach ($line in @([RmEngine]::ScanRosterIndex($bulkIdx, $secWant, 0))) {
                    $f = $line.Split('|')
                    if ($f.Count -lt 10) { continue }
                    $label = $null; $matchedCode = ''
                    $taxEnd = [math]::Min($f.Count - 1, 23)
                    foreach ($ti in @(8) + @(10..$taxEnd)) {
                        if ($ti -ge $f.Count) { break }
                        $code = $f[$ti]
                        if (-not $code) { continue }
                        $label = & $resolveTax $code ''
                        if ($label) { $matchedCode = $code; break }
                    }
                    if (-not $label) { continue }   # same therapy scope as the primary sweep
                    $primaryCode = Get-RmIndexPrimaryCode $f
                    $primaryInScope = [bool](& $resolveTax $primaryCode '')
                    if (-not $primaryInScope -and $script:RmGenericPrimaryTaxonomies.ContainsKey($primaryCode)) { $primaryInScope = $true }
                    $isOrg = $f[1] -eq '2'
                    $found2[$f[0]] = [pscustomobject]@{
                        NPI = $f[0]
                        Name = if ($isOrg) { $f[2] } else { ("$($f[4]) $($f[3])").Trim() }
                        Type = if ($isOrg) { 'Organization' } else { 'Individual' }
                        Taxonomy = Get-RmTaxonomyName $matchedCode
                        # The in-area SITE ZIP, not the registered one; city/
                        # state stay blank rather than showing the registered
                        # town next to a local ZIP.
                        City = ''; State = ''; Zip = $secZip[$f[0]]
                        Enumerated = if ($f[9] -match '^(\d{2})/(\d{2})/(\d{4})$') { "$($Matches[3])-$($Matches[1])-$($Matches[2])" } else { $f[9] }
                        PrimaryInScope = $primaryInScope
                        Presence = "secondary site (registered in $($f[5]), $($f[6]))"
                    }
                }
                Write-Verbose "Secondary-location sweep added providers (total now $($found2.Count))."
            }
        }
        Write-Verbose "Bulk sweep found $($found2.Count) provider(s)."
        # Only ADOPT the local answer when it actually covers the request.
        # An empty result means the monthly file predates these providers
        # (or the ZIP is new), so fall through to the live registry rather
        # than silently reporting "no providers here".
        if ($found2.Count -gt 0) { return @($found2.Values | Sort-Object Name) }
        Write-Verbose 'Bulk index had no providers for this area; falling back to the live registry.'
    }

    # Radius searches query each ZIP exactly (complete, and safely under
    # NPPES's 1,200-per-query ceiling) instead of one wide prefix.
    # @() around the WHOLE if: assignment from if{} unrolls one-element arrays
    # to a scalar, and .Count on a string throws under StrictMode (the same
    # gotcha the watchlist hit once).
    $zipQueries = @(if ($ZipList) { $ZipList | Where-Object { $_ -match '^\d{5}$' } | Sort-Object -Unique }
                    else { $Zip })
    $found = @{}
    $zqN = 0
    foreach ($zq in $zipQueries) {
    $zqN++
    if ($zipQueries.Count -gt 1) { Write-Verbose "NPPES sweep $zqN of $($zipQueries.Count): ZIP $zq" }
    $zipPrefix = $zq.TrimEnd('*')
    foreach ($term in $script:RmSearchTerms) {
        $skip = 0
        while ($true) {
            $q = 'postal_code={0}&taxonomy_description={1}&limit=200&skip={2}' -f
                [uri]::EscapeDataString($zq), [uri]::EscapeDataString($term), $skip
            $resp = Invoke-RmNppes $q
            $results = @(Get-RmProp $resp 'results')
            foreach ($r in $results) {
                $npi = [string](Get-RmProp $r 'number')
                if ($found.ContainsKey($npi)) { continue }

                # Code-level taxonomy filter (search terms are fuzzy — e.g.
                # "Physical Therapy" also returns PT Assistants, and
                # "Rehabilitation" returns physiatrists; both are excluded).
                $taxes = @(Get-RmProp $r 'taxonomies')
                $taxLabel = $null
                foreach ($tx in $taxes) {
                    $taxLabel = & $resolveTax ([string](Get-RmProp $tx 'code')) ([string](Get-RmProp $tx 'desc'))
                    if ($taxLabel) { break }
                }
                if (-not $taxLabel) { continue }
                # Scope of the PRIMARY taxonomy specifically - not of the
                # first in-scope one found. A provider can list the same
                # taxonomy twice with the primary flag on the SECOND entry,
                # which made a real PT read as secondary-only and vanish
                # from the competitive ranking.
                $primaryTx = @($taxes | Where-Object { (Get-RmProp $_ 'primary') -eq $true })
                $primaryInScope = $false
                if ($primaryTx.Count) {
                    $primaryInScope = [bool](& $resolveTax ([string](Get-RmProp $primaryTx[0] 'code')) ([string](Get-RmProp $primaryTx[0] 'desc')))
                    if (-not $primaryInScope -and
                        $script:RmGenericPrimaryTaxonomies.ContainsKey([string](Get-RmProp $primaryTx[0] 'code'))) {
                        $primaryInScope = $true   # see RmGenericPrimaryTaxonomies
                    }
                }

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
                Presence = ''
                    NPI        = $npi
                    Name       = $name
                    Type       = if ($isOrg) { 'Organization' } else { 'Individual' }
                    Taxonomy   = $taxLabel
                    City       = [string](Get-RmProp $loc[0] 'city')
                    State      = [string](Get-RmProp $loc[0] 'state')
                    Zip        = $postal.Substring(0, [Math]::Min(5, $postal.Length))
                    Enumerated = [string](Get-RmProp $basic 'enumeration_date')
                    PrimaryInScope = $primaryInScope
                }
            }
            if ($results.Count -lt 200) { break }
            if ($skip -ge 1000) {
                # NPPES refuses to page past skip=1000; a full last page means
                # there are probably more providers we cannot see.
                Write-Warning ("NPPES returned its maximum of 1,200 results for '$term' in '$zq' — " +
                    "the provider list may be incomplete. Use a narrower ZIP (a full 5-digit ZIP " +
                    "instead of a prefix) to make sure nothing is missed.")
                break
            }
            $skip += 200
        }
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
    .PARAMETER RequireEntity
      Same upgrade pattern for the Entity field ('1' individual / '2'
      organization) - the O&R at-risk check must never flag an organization,
      so it needs the entity type, not just the specialty label (orgs carry
      physician-style labels like 'Diagnostic Radiology Physician').
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Npi, [switch]$RequireZip, [switch]$RequireEntity)

    $cache = Read-RmNppesCache
    $result = @{}
    # Only look up well-formed NPIs — the source NPIs come from an external data
    # file and must never be concatenated into a URL or cache key unvalidated.
    $missing = @($Npi | Sort-Object -Unique |
        Where-Object { (Test-RmNpiShape $_) -and (
            -not $cache.ContainsKey($_) -or
            ($RequireZip -and $null -eq $cache[$_].PSObject.Properties['Zip']) -or
            ($RequireEntity -and $null -eq $cache[$_].PSObject.Properties['Entity'])) })

    # Local NPPES bulk index first (one streaming scan answers every miss at
    # once, offline); anything it cannot answer falls through to the live
    # registry exactly as before.
    $bulkIdx = Get-RmNppesIndexPath
    if ($missing.Count -gt 0 -and (Test-Path -LiteralPath $bulkIdx)) {
        $want = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($m in $missing) { [void]$want.Add($m) }
        Write-Verbose "NPPES bulk index: scanning for $($want.Count) provider(s)..."
        $dirtyBulk = $false
        foreach ($line in @([RmEngine]::ScanRosterIndex($bulkIdx, $want, 0))) {
            $f = $line.Split('|')
            if ($f.Count -lt 10) { continue }
            $isOrg = $f[1] -eq '2'
            $postal = $f[7]
            $cache[$f[0]] = [pscustomobject]@{
                Name      = if ($isOrg) { $f[2] } else { ("$($f[4]) $($f[3])").Trim() }
                Specialty = Get-RmTaxonomyName (Get-RmIndexPrimaryCode $f)
                City      = $f[5]; State = $f[6]
                Zip       = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { $postal }
                Entity    = $f[1]
                FetchedAt = [DateTime]::UtcNow.ToString('o')
            }
            $dirtyBulk = $true
        }
        if ($dirtyBulk) { Write-RmNppesCache $cache }
        $missing = @($missing | Where-Object { -not $cache.ContainsKey($_) -or
            ($RequireZip -and $null -eq $cache[$_].PSObject.Properties['Zip']) -or
            ($RequireEntity -and $null -eq $cache[$_].PSObject.Properties['Entity']) })
    }
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
                    Name = '(NPI deactivated or not found)'; Specialty = ''; City = ''; State = ''; Zip = ''
                    Entity = ''   # unknown: the at-risk check must NOT flag it
                    FetchedAt = [DateTime]::UtcNow.ToString('o') }
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
                    Entity    = if ($isOrg) { '2' } else { '1' }
                    FetchedAt = [DateTime]::UtcNow.ToString('o')
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
    $clinicTaxNote = 'Clinic list = NPPES providers with a REGISTERED practice location in the requested ZIP - plus, when the local NPPES index carries them, providers whose SECONDARY practice location is there (see the Presence column) - holding taxonomies: ' +
        (@($script:RmClinicTaxonomies.Values) +
         $(if (-not $OrganizationsOnly) {
             @($script:RmIndividualTaxonomyPrefixes.Values | ForEach-Object { "$_ (incl. subspecialties)" })
           } else { @() }) -join ', ') +
        '. Excluded on purpose: PT/OT/speech ASSISTANTS, physiatrists (physicians), cardiac and substance-use rehab, inpatient rehab units/hospitals.'
    if ($Info.Source -eq 'hop-teaming') {
        @(
            "Source: DocGraph Hop Teaming $y, produced by CareSet Systems from 100% of Medicare Fee-for-Service Part A and Part B claims (data 'DocGraph' from CareSet; CC BY-NC-SA 4.0 non-commercial license unless you hold a commercial license from CareSet)."
            "The file covers services from $y-01-01 to $y-12-31. It shows the structure of the referral market in $y, NOT this year's volumes."
            'SharedPatients in the SOURCES table = patient_count: distinct Medicare FFS patients who saw the source provider and then that clinic (a directed shared-patient "hop"; a referral proxy, not billed referrals). SharedEvents = transaction_count: total from->to switches, so one patient bouncing back and forth counts each time.'
            'SharedPatients in the CLINICS table = the SUM of those per-source counts, NOT a unique-patient total: a patient sent by three sources is counted three times. Treat it as relative referral VOLUME, not a headcount of distinct patients.'
            "Pairs sharing fewer than 11 distinct patients in $y are excluded per CMS privacy policy, so low-volume referrers are invisible."
            'AvgDayWait = average days from the source visit to the clinic visit. Short waits (days-weeks) look like referrals; waits of months look like loosely-related care. Direction is claims sequence, not a literal referral: CareSet notes a "referee" can appear to send patients to their "referrer". Judge pairs by specialty — an orthopedic surgeon feeding a PT is referral-like; a lab is not.'
            'PARTICIPATION: these volumes come from Medicare FFS CLAIMS, not from quality-program reporting - whether a provider participates in MIPS (or any CMS quality program) has no effect on whether they appear here. Provider DISCOVERY comes from NPPES, which lists every provider with an NPI regardless of Medicare participation - so the provider list is complete even where the measured volume is zero.'
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
            'PARTICIPATION: these volumes come from Medicare FFS CLAIMS, not from quality-program reporting - whether a provider participates in MIPS (or any CMS quality program) has no effect on whether they appear here. Provider DISCOVERY comes from NPPES, which lists every provider with an NPI regardless of Medicare participation - so the provider list is complete even where the measured volume is zero.'
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
        [ValidateRange(0, 100)][double]$RadiusMiles = 0,
        [switch]$OrganizationsOnly,
        [switch]$SkipEnrichment,   # NPI-only output when offline
        [string]$CentroidPath      # test override for the radius table
    )

    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw ("No shared-patient dataset is available yet. Either click 'Download CMS dataset' " +
               "(free 2015 data, ~356 MB) or import a CareSet DocGraph Hop Teaming file " +
               "with 'Import CareSet file' / Import-RmDataset.")
    }
    $dataset = $info.Path
    $isHop = $info.Source -eq 'hop-teaming'

    # Radius mode: turn the center ZIP into the full list of ZIPs whose
    # centroid falls inside the circle, then sweep NPPES per ZIP (complete —
    # no reliance on prefix shapes or the 1,200-result prefix ceiling).
    $radiusZips = $null
    $centerLoc = $null
    if ($RadiusMiles -gt 0) {
        if ($Zip -notmatch '^\d{5}$') {
            throw "A radius search needs a full 5-digit center ZIP (got '$Zip'); prefixes like 630* only work with radius 0."
        }
        $radiusZips = @(Get-RmZipsInRadius -Zip $Zip -RadiusMiles $RadiusMiles -CentroidPath $CentroidPath)
        $cents = Get-RmCentroids $CentroidPath
        $centerLoc = $cents[$Zip]
        Write-Verbose "Radius $RadiusMiles mi around $Zip covers $($radiusZips.Count) ZIP(s)."
    }

    Write-Verbose "Finding rehab providers via NPPES..."
    $clinics = if ($radiusZips) {
        @(Find-RmClinic -ZipList $radiusZips -OrganizationsOnly:$OrganizationsOnly)
    } else {
        @(Find-RmClinic -Zip $Zip -OrganizationsOnly:$OrganizationsOnly)
    }
    if ($clinics.Count -eq 0) {
        throw ("NPPES lists no outpatient rehab providers (PT/rehab clinics" +
               $(if (-not $OrganizationsOnly) { ", individual PT/OT/SLPs" }) +
               ") with a practice location in " +
               $(if ($radiusZips) { "the $RadiusMiles-mile radius around ZIP '$Zip'. Try a larger radius." }
                 else { "ZIP '$Zip'. Try a broader prefix like '$($Zip.Substring(0,3))*' or a radius search." }))
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
            Presence        = if ($null -ne $_.PSObject.Properties['Presence']) { [string]$_.Presence } else { '' }
        }
        if ($null -ne $centerLoc) {
            $cents2 = Get-RmCentroids $CentroidPath
            # Closest KNOWN location (row ZIP + registered secondary sites),
            # so a multi-site provider's distance is to its nearest clinic,
            # not to wherever it happens to be enrolled.
            $dmin = Get-RmClosestSiteDistance -Npi $_.NPI -RowZip ([string]$_.Zip) -RefLoc $centerLoc -Cents $cents2
            $row['DistanceMiles'] = if ($null -ne $dmin) { $dmin } else { '' }
        }
        $row['ReferralSources'] = if ($agg) { $agg.Sources } else { 0 }
        $row['SharedPatients']  = if ($agg) { $agg.Benes } else { 0 }
        # Is this NPI one site, or a billing NPI covering many? Registered
        # secondary locations prove multi-site; scale infers it when the
        # registry is silent (chains often register none).
        $secLoc = Get-RmSecondaryLocationCount $_.NPI
        $srcN = if ($agg) { $agg.Sources } else { 0 }
        $row['PracticeSites'] = if ($secLoc -gt 0) { $secLoc + 1 } else { 1 }
        # Clinicians on the org's Care Compare roster under this NAME (all
        # its locations combined); blank for individuals and for orgs the
        # roster does not list.
        $row['RosterSize'] = $(if ($_.Type -eq 'Organization') {
            $rs = Get-RmCareCompareRosterSize $_.Name
            if ($rs -gt 0) { $rs } else { '' }
        } else { '' })
        $row['MultiSiteNPI'] = if ($secLoc -gt 0) { 'Yes (registry)' }
                               elseif ($srcN -ge $script:RmSingleSiteSourceCeiling) { 'Likely (scale)' }
                               else { '' }
        # Chain marker. Only organizations can carry it - an individual
        # therapist's name is not a company name.
        $row['Chain'] = if ($_.Type -eq 'Organization') { Get-RmChainMark $_.Name } else { '' }
        if (-not $isHop) { $row['SameDay'] = if ($agg) { $agg.SameDay } else { 0 } }
        $row['ExistedInDataYear'] = $existed
        [pscustomobject]$row
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'Name'; Descending = $false})
    $notYetEnumerated = @($clinicRows | Where-Object { $_.ExistedInDataYear -like 'No*' }).Count
    $rosterSized = @($clinicRows | Where-Object { $_.RosterSize -ne '' }).Count
    $rosterNote = if ($rosterSized -gt 0) {
        'ROSTER SIZE: distinct clinicians listed on Medicare Care Compare under that organization NAME - all of its locations combined, so a chain shows its national roster. Blank = an individual provider, or an organization Care Compare does not list (cash-pay and recently enrolled clinics are missing there).'
    } else { $null }
    $nameRescueRows = @($clinicRows | Where-Object { $_.Presence -like 'therapy-named*' }).Count
    $nameRescueNote = if ($nameRescueRows -gt 0) {
        "NAME-MATCHED PRACTICES: $nameRescueRows organization(s) in this list registered NO therapy taxonomy at all, but their legal or doing-business-as name says they are a PT/OT/SLP practice (most hold the legacy 'Specialist' code or a generic clinic code - measured nationwide, 469 such practices carried 785,074 patients in 2022). They are included and ranked, marked in the Presence column; facility types that merely mention therapy (hospitals, nursing, home health) are refused."
    } else { $null }
    $secondaryRows = @($clinicRows | Where-Object { $_.Presence -like 'secondary site*' }).Count
    $secondaryNote = if ($secondaryRows -gt 0) {
        "SECONDARY SITES: $secondaryRows provider(s) in this list are REGISTERED outside the searched area but operate a practice location inside it (from NPPES's secondary-practice-location file; marked in the Presence column with their registered city). Their referral volume is measured on the NPI, so it covers ALL of that provider's locations, not just the local one - use the Multi-site chains tab for per-location figures."
    } else { $null }

    # A ZIP full of therapists but almost no measured volume looks exactly
    # like a broken search. Explain it instead: state the coverage plainly
    # and, when we can, name the local Medicare market that caused it.
    $withVol = @($clinicRows | Where-Object { [int]$_.SharedPatients -gt 0 }).Count
    $coverageNote = $null
    if ($clinicRows.Count -ge 5 -and $withVol -le [math]::Ceiling($clinicRows.Count * 0.25)) {
        $mkt = $null
        try {
            $z5note = if ($Zip -match '^\d{5}$') { $Zip } elseif ($radiusZips) { [string]@($radiusZips)[0] } else { '' }
            if ($z5note) { $mkt = Get-RmCountyMarket -Zip $z5note }
        } catch { $mkt = $null }
        $coverageNote = ("LOW MEASURED VOLUME: $withVol of $($clinicRows.Count) providers found here have ANY measured referral pair in $($info.Year). " +
            "The provider list is complete — this is the DATA being thin, not the search. Two reasons dominate: " +
            "(1) pairs under 11 shared patients are deleted before publication, which erases most relationships in a low-volume area; " +
            "(2) the file covers Medicare fee-for-service only.") +
            $(if ($mkt) { " In $($mkt.County), $($mkt.State), $($mkt.MaPct)% of $('{0:N0}' -f $mkt.TotalBenes) Medicare beneficiaries are in Medicare Advantage and invisible here, leaving about $('{0:N0}' -f $mkt.FfsBenes) fee-for-service beneficiaries countywide." }) +
            " Try a radius search to see the wider market."
    }

    $multiSite = @($clinicRows | Where-Object { $_.MultiSiteNPI })
    $multiNote = $null
    if ($multiSite.Count) {
        $top3 = @($multiSite | Sort-Object SharedPatients -Descending | Select-Object -First 3 |
            ForEach-Object { "$($_.Name) ($('{0:N0}' -f $_.SharedPatients) patients from $('{0:N0}' -f $_.ReferralSources) sources)" })
        $multiNote = ("MULTI-SITE NPIs: $($multiSite.Count) provider(s) here bill under an NPI that covers MORE THAN ONE location, " +
            "so their volume is the whole footprint of that NPI, not this address: $($top3 -join '; ')" +
            $(if ($multiSite.Count -gt 3) { ', ...' }) + '. ' +
            "The MultiSiteNPI column says 'Yes (registry)' when NPPES lists extra practice locations, or 'Likely (scale)' when the NPI draws " +
            "$($script:RmSingleSiteSourceCeiling)+ distinct referring providers - far more than one outpatient site plausibly has. " +
            'Shared-patient data carries no service address, so per-location volume cannot be derived from it.')
    }

    $chained = @($clinicRows | Where-Object { $_.Chain })
    # Same-company clusters hiding behind per-clinic legal names.
    $sibs = @(Get-RmNameSiblingGroups -Rows $clinicRows)
    $sibNote = $null
    if ($sibs.Count) {
        $top = @($sibs | Select-Object -First 3 | ForEach-Object {
            "'$($_.LeadWord)' - $($_.Organizations) organizations, $('{0:N0}' -f $_.CombinedPatients) patients between them ($(@($_.Names | Select-Object -First 4) -join ', ')$(if (@($_.Names).Count -gt 4) { ', ...' }))"
        })
        $sibNote = ("POSSIBLE SAME COMPANY: some groups register EVERY clinic under its own legal name, so one practice " +
            "appears as several modest rows instead of one large one. Organizations here sharing a leading name word: " +
            ($top -join '; ') + $(if ($sibs.Count -gt 3) { "; and $($sibs.Count - 3) more" }) + '. ' +
            "Real case: Advanced Training and Rehab in St Louis enrolls ATR JUSTIN LLC, ATR RYAN LLC, ATR JEFF LLC and more. " +
            "This is a prompt to check, NOT a finding - nothing has been combined, and unrelated practices can share a word. " +
            "To combine them deliberately, paste their NPIs together into Source analysis, or use the Multi-site chains tab.")
    }
    $notes = @(Get-RmMethodologyNotes -Info $info -OrganizationsOnly:$OrganizationsOnly) + @(
        $(if ($secondaryNote) { $secondaryNote })
        $(if ($nameRescueNote) { $nameRescueNote })
        $(if ($rosterNote) { $rosterNote })
        $(if ($sibNote) { $sibNote })
        $(if ($coverageNote) { $coverageNote })
        $(if ($multiNote) { $multiNote })
        $(if ($chained.Count) {
            (Get-RmChainNote) + " Flagged here: $($chained.Count) of $($clinicRows.Count) providers - " +
            (@($chained | Sort-Object SharedPatients -Descending | Select-Object -First 3 | ForEach-Object {
                $d = Get-RmChainDetail $_.Name; "$($_.Name) ($($d.Npis) org NPIs in $($d.Cities) cities)" }) -join '; ') +
            $(if ($chained.Count -gt 3) { ', ...' }) + '.'
        })
        $(if ($radiusZips) { "RADIUS SEARCH: providers were swept from the $($radiusZips.Count) ZIP code(s) whose US-Census area centroid lies within $RadiusMiles straight-line miles of ZIP $Zip's centroid. DistanceMiles is centroid-to-centroid, not driving distance, and for a multi-site provider it is measured to its CLOSEST known location (registered address or any secondary practice location); PO-box-only ZIPs (absent from the Census table) are not swept - measured nationwide that hides 0.5% of therapy NPIs from radius sweeps (an exact-ZIP or prefix search still finds them)." })
        $(if ($notYetEnumerated -gt 0) { '{0} of {1} providers found in this ZIP were issued their NPI after the {2} file''s service window ended, so they cannot appear in it (ExistedInDataYear = No).' -f $notYetEnumerated, $clinicRows.Count, $info.Year })
        $(if ($enrichNote) { $enrichNote })
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Zip     = if ($radiusZips) { "$Zip+${RadiusMiles}mi" } else { $Zip }
        Clinics = $clinicRows
        Sources = $sources
        ProvidersWithVolume = $withVol
        CoverageNote = $coverageNote      # $null unless volume is thin
        ChainClinics = $chained.Count
        SiblingGroups = $sibs             # possible one-company-many-names clusters
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
    # A source who is a member of the SAME bucket is an internal handoff
    # (PT->OT co-treatment), not an external referral - same rule as the
    # single-practice analysis. Members of OTHER buckets stay: a competitor
    # group sending patients here is a real external source.
    $buckets = @{}
    foreach ($e in $edges) {
        $srcBuckets = @($TargetToBucket[$e.SourceNpi])
        foreach ($label in @($TargetToBucket[$e.TargetNpi])) {
            if (-not $label) { continue }
            $lbl = [string]$label
            if ($srcBuckets -contains $lbl) { continue }   # internal to this bucket
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

function Get-RmGroupBenchmark {
    <#
    .SYNOPSIS
      Group-level referral benchmark: rolls inbound shared-patient volume up
      from individual member NPIs to their PRACTICE GROUPS, ranks the groups,
      and returns the full source->group edge list so each group's feeders
      (and the sources it is missing) can be examined and exported.
    .PARAMETER TargetToBucket
      Hashtable: member NPI (string) -> group key, or a LIST of group keys
      when one NPI belongs to several groups (credited to each). Use a stable
      unique key (the group PAC ID), not a display name.
    .PARAMETER BucketNames
      Optional hashtable: group key -> display name for the output rows.
    .NOTES
      Rank and share compare the buckets you passed in — typically each
      group's IN-ZIP members — so a national chain is measured by its local
      presence, not its nationwide roster.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][hashtable]$TargetToBucket,
        [hashtable]$BucketNames = @{},
        [switch]$SkipEnrichment
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $targets = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($k in $TargetToBucket.Keys) { [void]$targets.Add([string]$k) }
    if ($targets.Count -eq 0) {
        return [pscustomobject]@{ Year = $info.Year; Label = $info.Label; Buckets = @(); Edges = @(); OutboundEdges = @(); Notes = @() }
    }
    # ONE both-directions pass: rows where a member RECEIVED (inbound — the
    # ranking basis) and rows where a member was seen FIRST (outbound — where
    # the groups send patients onward). Same scan cost as inbound alone.
    $all = [RmEngine]::ScanEither($info.Path, $targets, $info.Format)
    $edges = @($all | Where-Object { $targets.Contains($_.TargetNpi) })
    $outEdges = @($all | Where-Object { $targets.Contains($_.SourceNpi) })

    # bucket -> aggregate; (bucket, source) -> patients + distinct members fed.
    # Same-bucket sources are internal handoffs (PT->OT inside one group) and
    # are excluded from the group's inbound figures - the ranking must compare
    # EXTERNAL referral pull, and a big group would otherwise out-rank a small
    # one partly on its own internal co-treatment. Members of OTHER listed
    # groups still count: that is real cross-group flow.
    $buckets = @{}
    $bySrc = @{}
    foreach ($e in $edges) {
        $srcBuckets = @($TargetToBucket[$e.SourceNpi])
        foreach ($label in @($TargetToBucket[$e.TargetNpi])) {
            if (-not $label) { continue }
            $lbl = [string]$label
            if ($srcBuckets -contains $lbl) { continue }   # internal to this bucket
            if (-not $buckets.ContainsKey($lbl)) {
                $buckets[$lbl] = [pscustomobject]@{
                    Patients = 0
                    Sources = (New-Object 'System.Collections.Generic.HashSet[string]')
                    Members = (New-Object 'System.Collections.Generic.HashSet[string]')
                }
            }
            $b = $buckets[$lbl]
            $b.Patients += $e.BeneCount
            [void]$b.Sources.Add($e.SourceNpi)
            [void]$b.Members.Add($e.TargetNpi)
            $key = $lbl + '|' + $e.SourceNpi
            if (-not $bySrc.ContainsKey($key)) {
                $bySrc[$key] = [pscustomobject]@{
                    Bucket = $lbl; SourceNpi = $e.SourceNpi; Patients = 0
                    Members = (New-Object 'System.Collections.Generic.HashSet[string]')
                }
            }
            $bySrc[$key].Patients += $e.BeneCount
            [void]$bySrc[$key].Members.Add($e.TargetNpi)
        }
    }

    # (bucket, destination) -> patients + distinct members SENDING (outbound).
    $byDst = @{}
    foreach ($e in $outEdges) {
        foreach ($label in @($TargetToBucket[$e.SourceNpi])) {
            if (-not $label) { continue }
            $lbl = [string]$label
            $key = $lbl + '|' + $e.TargetNpi
            if (-not $byDst.ContainsKey($key)) {
                $byDst[$key] = [pscustomobject]@{
                    Bucket = $lbl; DestNpi = $e.TargetNpi; Patients = 0
                    Members = (New-Object 'System.Collections.Generic.HashSet[string]')
                }
            }
            $byDst[$key].Patients += $e.BeneCount
            [void]$byDst[$key].Members.Add($e.SourceNpi)
        }
    }

    # Enrich names/specialties for sources AND destinations, biggest first
    # (cached on disk; one shared cap).
    $detail = @{}
    if (-not $SkipEnrichment -and ($bySrc.Count -gt 0 -or $byDst.Count -gt 0)) {
        $volBySrc = @{}
        foreach ($v in $bySrc.Values) {
            if (-not $volBySrc.ContainsKey($v.SourceNpi)) { $volBySrc[$v.SourceNpi] = 0 }
            $volBySrc[$v.SourceNpi] += $v.Patients
        }
        foreach ($v in $byDst.Values) {
            if (-not $volBySrc.ContainsKey($v.DestNpi)) { $volBySrc[$v.DestNpi] = 0 }
            $volBySrc[$v.DestNpi] += $v.Patients
        }
        $enrichList = @($volBySrc.GetEnumerator() | Sort-Object Value -Descending |
            Select-Object -First $script:RmConfig.EnrichCap | ForEach-Object { $_.Key })
        if ($volBySrc.Count -gt @($enrichList).Count) {
            Write-Warning ("Named the top $(@($enrichList).Count) of $($volBySrc.Count) distinct sources " +
                "by volume; the rest show NPI only. Raise with Set-RmConfig -EnrichCap.")
        }
        $detail = Get-RmProviderDetail -Npi @($enrichList)
    }

    # Ranked group table with share of the measured group volume.
    $total = 0; foreach ($b in $buckets.Values) { $total += $b.Patients }
    $bucketRows = New-Object System.Collections.Generic.List[object]
    $rank = 0
    foreach ($lbl in ($buckets.Keys | Sort-Object -Property @{Expression = { $buckets[$_].Patients }; Descending = $true},
                                                            @{Expression = { $_ }; Descending = $false})) {
        $rank++
        $b = $buckets[$lbl]
        $bucketRows.Add([pscustomobject]@{
            Rank            = $rank
            Bucket          = $lbl
            GroupName       = if ($BucketNames.ContainsKey($lbl)) { [string]$BucketNames[$lbl] } else { $lbl }
            InboundPatients = $b.Patients
            SharePct        = if ($total -gt 0) { [math]::Round(100.0 * $b.Patients / $total, 1) } else { 0 }
            Sources         = $b.Sources.Count
            MembersWithVolume = $b.Members.Count
        })
    }

    $edgeRows = @($bySrc.Values | ForEach-Object {
        $d = if ($detail.ContainsKey($_.SourceNpi)) { $detail[$_.SourceNpi] } else { $null }
        [pscustomobject]@{
            Bucket          = $_.Bucket
            GroupName       = if ($BucketNames.ContainsKey($_.Bucket)) { [string]$BucketNames[$_.Bucket] } else { $_.Bucket }
            SourceNPI       = $_.SourceNpi
            SourceName      = if ($d) { $d.Name } else { '' }
            SourceSpecialty = if ($d) { $d.Specialty } else { '' }
            SharedPatients  = $_.Patients
            MembersFed      = $_.Members.Count
        }
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'SourceNPI'; Descending = $false})

    $outRows = @($byDst.Values | ForEach-Object {
        $d = if ($detail.ContainsKey($_.DestNpi)) { $detail[$_.DestNpi] } else { $null }
        [pscustomobject]@{
            Bucket          = $_.Bucket
            GroupName       = if ($BucketNames.ContainsKey($_.Bucket)) { [string]$BucketNames[$_.Bucket] } else { $_.Bucket }
            DestNPI         = $_.DestNpi
            DestName        = if ($d) { $d.Name } else { '' }
            DestSpecialty   = if ($d) { $d.Specialty } else { '' }
            SharedPatients  = $_.Patients
            MembersSending  = $_.Members.Count
        }
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'DestNPI'; Descending = $false})

    $notes = @(Get-RmMethodologyNotes -Info $info) + @(
        ''
        "GROUP BENCHMARK METHOD: inbound shared-patient volume of each group's member therapist NPIs (as passed in — typically the IN-ZIP members), summed per group on $($info.Label). Handoffs BETWEEN members of the same group (PT→OT co-treatment) are internal care, not referrals, and are excluded from InboundPatients, Sources, and the ranking; flow from members of a DIFFERENT listed group still counts as external."
        'Rank and SharePct compare the listed groups against each other; the share is of MEASURED group volume (sum semantics: a patient sent by three sources counts three times).'
        'MembersFed = how many of the group''s member therapists that source fed (11+ patient pairs each). A source feeding several members is a deep relationship, not a fluke.'
        'A group''s ORGANIZATION NPI can carry additional volume not shown here (benchmark it separately on the Practice benchmark tab); solo therapists without a group are not in this table.'
        'OUTBOUND rows = where the groups'' members were seen FIRST and the patient went ONWARD (post-therapy hand-offs: physicians, imaging, hospitals — and sometimes members of the same or another listed group, which is internal continuity of care, not a referral out). MembersSending = distinct member therapists sending to that destination.'
    ) | Where-Object { $null -ne $_ }

    # .ToArray(), not @($list) — see the trend function: the @() binder can
    # throw a spurious 'Argument types do not match' on a generic List here.
    [pscustomobject]@{
        Year          = $info.Year
        Label         = $info.Label
        Buckets       = $bucketRows.ToArray()
        Edges         = $edgeRows
        OutboundEdges = $outRows
        Notes         = @($notes)
    }
}

function Get-RmGroupTrend {
    <#
    .SYNOPSIS
      Year-over-year referral trend for a PRACTICE GROUP: the group's member
      therapist NPIs are scanned across every imported DocGraph Hop Teaming
      year and rolled up per year — inbound volume, distinct sources, members
      with volume, and each year's top sources.
    .NOTES
      Hop Teaming years only (the CMS 2015 file uses a different window and
      is excluded so the trend stays honest); needs 2+ imported years. One
      full file scan per year — expect minutes per year. Pass the same member
      set you benchmark with (typically the group's IN-ZIP members) and note
      that roster CHANGES over time are invisible here: today's members are
      scanned in every year, so a therapist who joined in 2021 contributes
      zeros before that.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateCount(1, 10000)][string[]]$MemberNpi,
        [string]$GroupName = '',
        [int]$TopSources = 3,
        [switch]$SkipEnrichment
    )
    $years = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' } | Sort-Object Year)
    if ($years.Count -lt 2) {
        throw ("A trend needs at least TWO imported Hop Teaming years (found $($years.Count)). " +
               "Import more years with 'Import CareSet file' / Import-RmDataset.")
    }
    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($n in $MemberNpi) { if (Test-RmNpiShape $n) { [void]$set.Add($n) } }
    if ($set.Count -eq 0) { throw 'No well-formed member NPIs were provided.' }

    $rows = New-Object System.Collections.Generic.List[object]
    $allTopNpis = New-Object 'System.Collections.Generic.HashSet[string]'
    $perYearTop = @{}
    foreach ($y in $years) {
        Write-Verbose "Scanning $($y.Label) for $($set.Count) member NPIs..."
        $edges = @([RmEngine]::ScanInbound($y.Path, $set, [RmEngine]::FormatHopTeaming))
        $vol = 0
        $srcs = New-Object 'System.Collections.Generic.HashSet[string]'
        $members = New-Object 'System.Collections.Generic.HashSet[string]'
        $bySrc = @{}
        foreach ($e in $edges) {
            # Member-to-member handoffs are internal, not inbound referrals -
            # the same exclusion the single-practice analysis and trend apply.
            if ($set.Contains($e.SourceNpi)) { continue }
            $vol += $e.BeneCount
            [void]$srcs.Add($e.SourceNpi)
            [void]$members.Add($e.TargetNpi)
            if (-not $bySrc.ContainsKey($e.SourceNpi)) { $bySrc[$e.SourceNpi] = 0 }
            $bySrc[$e.SourceNpi] += $e.BeneCount
        }
        $top = @($bySrc.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First $TopSources)
        $perYearTop[$y.Year] = $top
        foreach ($t in $top) { [void]$allTopNpis.Add($t.Key) }
        $rows.Add([pscustomobject]@{
            Year              = $y.Year
            InboundSources    = $srcs.Count
            InboundPatients   = $vol
            MembersWithVolume = $members.Count
            TopSources        = ''   # filled after enrichment below
        })
    }

    $detail = @{}
    if (-not $SkipEnrichment -and $allTopNpis.Count -gt 0) {
        $detail = Get-RmProviderDetail -Npi @($allTopNpis)
    }
    foreach ($r in $rows) {
        $tops = foreach ($t in $perYearTop[$r.Year]) {
            $d = if ($detail.ContainsKey($t.Key)) { $detail[$t.Key] } else { $null }
            $nm = if ($d -and $d.Name) { $d.Name } else { $t.Key }
            "$nm ($($t.Value))"
        }
        $r.TopSources = @($tops) -join '; '
    }

    $yearArr = foreach ($y in $years) { $y.Year }
    $notes = @(
        "Source: DocGraph Hop Teaming years $($yearArr -join ', '), produced by CareSet Systems from Medicare FFS Part A+B claims (data 'DocGraph' from CareSet)."
        "Group trend for$(if ($GroupName) { " $GroupName —" }) $($set.Count) member therapist NPIs, rolled up per year: InboundPatients = shared patients INTO any member from OUTSIDE the group (member-to-member handoffs are internal care and excluded, matching the analysis tabs); InboundSources = distinct outside feeding providers; MembersWithVolume = members with any measured outside volume that year."
        'The member list is TODAY''s roster applied to every year — a therapist who joined recently contributes zeros in earlier years (roster churn is invisible in this data), and pairs under 11 patients are excluded in every year.'
        'Medicare FFS only: Medicare Advantage growth pulls patients out of this data over time — decline can reflect MA shift as well as lost referrals. The CMS 2015 file is intentionally excluded (different window/methodology).'
    )
    # .ToArray()/explicit array — see Get-RmProviderTrend: @(List) can trip the
    # engine's to-object-array binder here.
    [pscustomobject]@{
        GroupName = $GroupName
        Members   = $set.Count
        Years     = [int[]]$yearArr
        Rows      = $rows.ToArray()
        Notes     = @($notes)
    }
}

function Get-RmGroupMissedSources {
    <#
    .SYNOPSIS
      From a Get-RmGroupBenchmark edge list: the sources feeding OTHER groups
      with NO measured pair into the given group — the group-level outreach
      list. "Missed" can also mean a pair exists but fell under the 11-patient
      privacy floor.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Edges,
        [Parameter(Mandatory)][string]$Bucket
    )
    $mine = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($e in @($Edges | Where-Object { $_.Bucket -eq $Bucket })) { [void]$mine.Add($e.SourceNPI) }
    $agg = @{}
    foreach ($e in @($Edges | Where-Object { $_.Bucket -ne $Bucket })) {
        if ($mine.Contains($e.SourceNPI)) { continue }
        if (-not $agg.ContainsKey($e.SourceNPI)) {
            $agg[$e.SourceNPI] = [pscustomobject]@{
                SourceNPI = $e.SourceNPI; SourceName = $e.SourceName
                SourceSpecialty = $e.SourceSpecialty
                PatientsToOtherGroups = 0
                Groups = (New-Object 'System.Collections.Generic.HashSet[string]')
            }
        }
        $agg[$e.SourceNPI].PatientsToOtherGroups += [int]$e.SharedPatients
        [void]$agg[$e.SourceNPI].Groups.Add($e.Bucket)
    }
    @($agg.Values | ForEach-Object {
        [pscustomobject]@{
            SourceNPI             = $_.SourceNPI
            SourceName            = $_.SourceName
            SourceSpecialty       = $_.SourceSpecialty
            PatientsToOtherGroups = $_.PatientsToOtherGroups
            GroupsFed             = $_.Groups.Count
        }
    } | Sort-Object -Property @{Expression = 'PatientsToOtherGroups'; Descending = $true},
                              @{Expression = 'SourceNPI'; Descending = $false})
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
        # Keep the shape identical to the swept rows so the ranking table has
        # no ragged column.
        $row['Chain'] = if ($isOrg) { Get-RmChainMark $pracName } else { '' }
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
    # Competition ("1224") ranking, matching the analysis-report landscape:
    # equal volumes share a rank, so a zero-volume practice reads as tied
    # with its equals rather than placed by the alphabet.
    $ranked = @($clinics | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                                 @{Expression = 'Name'; Descending = $false})
    $rank = 0; $total = 0
    $curRank = 0; $prevVol = -1
    for ($i = 0; $i -lt $ranked.Count; $i++) {
        $total += [int]$ranked[$i].SharedPatients
        if ([int]$ranked[$i].SharedPatients -ne $prevVol) { $curRank = $i + 1; $prevVol = [int]$ranked[$i].SharedPatients }
        if ($ranked[$i].NPI -eq $Npi) { $rank = $curRank }
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

function Get-RmZipsInRadius {
    <#
    .SYNOPSIS
      All ZIP codes whose Census-centroid falls within the given radius of a
      center ZIP's centroid (straight-line miles). The center ZIP is always
      included. Radius 0 returns just the center ZIP.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{5}$')][string]$Zip,
        [Parameter(Mandatory)][ValidateRange(0, 100)][double]$RadiusMiles,
        [string]$CentroidPath
    )
    $centroids = Get-RmCentroids $CentroidPath
    if (-not $centroids.ContainsKey($Zip)) {
        throw ("ZIP $Zip is not in the Census ZIP-area table (PO-box-only and very new ZIPs are missing). " +
               "Try a neighboring ZIP with street addresses.")
    }
    if ($RadiusMiles -le 0) { return @($Zip) }
    $c = $centroids[$Zip]
    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($k in $centroids.Keys) {
        if ((Get-RmMilesBetween $c[0] $c[1] $centroids[$k][0] $centroids[$k][1]) -le $RadiusMiles) {
            $hits.Add($k)
        }
    }
    @($hits.ToArray() | Sort-Object)
}

function Get-RmUnderservedAreas {
    <#
    .SYNOPSIS
      Expansion-siting screen: for every county touched by a ZIP-radius
      sweep, the CURRENT registered PT/OT/SLP clinician headcount inside the
      sweep (local NPPES index) against the county's Original-Medicare (FFS)
      beneficiary population (CMS enrollment). A low therapists-per-10k
      figure marks a market with more Medicare demand per clinician.
    .NOTES
      The supply figure counts INDIVIDUAL clinicians registered in the swept
      ZIPs (any therapy taxonomy slot, the Practice-groups rule); the
      denominator is the WHOLE county, so the rate is only meaningful where
      CoveragePct is high - partially swept counties are shown but flagged.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{5}$')][string]$Zip,
        [Parameter(Mandatory)][ValidateRange(5, 100)][double]$RadiusMiles,
        [string]$CentroidPath,
        [string]$CrosswalkPath
    )
    $idx = Get-RmNppesIndexPath
    if (-not (Test-Path -LiteralPath $idx)) {
        throw ('The underserved-area screen needs the local NPPES index for the clinician headcount. ' +
               'One-time setup: import the NPPES bulk zip on the Multi-site chains tab (about 10 minutes), then run this again.')
    }
    $zips = @(Get-RmZipsInRadius -Zip $Zip -RadiusMiles $RadiusMiles -CentroidPath $CentroidPath)
    $zipSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($z in $zips) { [void]$zipSet.Add($z) }

    # ZIP -> county, plus each county's TOTAL ZIP count so partial sweeps
    # are visible (whole-county denominator vs sweep-only numerator).
    $xwPath = if ($CrosswalkPath) { $CrosswalkPath } else { Join-Path $PSScriptRoot 'zcta-county.csv' }
    if (-not (Test-Path -LiteralPath $xwPath)) { throw "ZIP-to-county table not found at '$xwPath'." }
    $zipFips = @{}
    $fipsZipTotal = @{}
    $reader = New-Object System.IO.StreamReader($xwPath)
    try {
        [void]$reader.ReadLine()   # header
        while ($null -ne ($line = $reader.ReadLine())) {
            $f = $line.Split(',')
            if ($f.Length -lt 2) { continue }
            if ($fipsZipTotal.ContainsKey($f[1])) { $fipsZipTotal[$f[1]]++ } else { $fipsZipTotal[$f[1]] = 1 }
            if ($zipSet.Contains($f[0])) { $zipFips[$f[0]] = $f[1] }
        }
    } finally { $reader.Dispose() }

    # One index sweep: individual clinicians with ANY therapy taxonomy slot
    # (the Practice-groups rule) registered in the swept ZIPs.
    $thByFips = @{}
    $sweptByFips = @{}
    $countyZip = @{}    # a sample swept ZIP per county, for the market lookup
    foreach ($kv in $zipFips.GetEnumerator()) {
        if ($sweptByFips.ContainsKey($kv.Value)) { $sweptByFips[$kv.Value]++ } else { $sweptByFips[$kv.Value] = 1; $countyZip[$kv.Value] = $kv.Key }
    }
    $thTotal = 0
    $emptyPrefixes = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in @([RmEngine]::ScanIndexByZip($idx, 7, $zipSet, $emptyPrefixes))) {
        $f = $line.Split('|')
        if ($f.Count -lt 9 -or $f[1] -ne '1') { continue }
        # NOT @(10..$max): when a short row makes $max < 10, PowerShell's
        # range operator counts DOWN and indexes past the array.
        $isTher = $false
        $slotMax = [math]::Min(23, $f.Count - 1)
        for ($slot = 8; $slot -le $slotMax; $slot++) {
            if ($slot -eq 9) { continue }   # field 9 is the enumeration date
            $code = $f[$slot]
            if ($code -and ($code.StartsWith('2251') -or $code.StartsWith('225X') -or $code.StartsWith('235Z'))) { $isTher = $true; break }
        }
        if (-not $isTher) { continue }
        $z5 = $f[7]
        if ($z5.Length -gt 5) { $z5 = $z5.Substring(0, 5) }
        if (-not $zipFips.ContainsKey($z5)) { continue }   # swept ZIP with no county row
        $fp = $zipFips[$z5]
        if ($thByFips.ContainsKey($fp)) { $thByFips[$fp]++ } else { $thByFips[$fp] = 1 }
        $thTotal++
    }

    $rowList = New-Object System.Collections.Generic.List[object]
    $noMarket = 0
    foreach ($fp in $sweptByFips.Keys) {
        $mkt = $null
        try { $mkt = Get-RmCountyMarket -Zip $countyZip[$fp] -CrosswalkPath $CrosswalkPath } catch { }
        if (-not $mkt) { $noMarket++ }
        $ffs = if ($mkt) { [int]$mkt.FfsBenes } else { 0 }
        $th = if ($thByFips.ContainsKey($fp)) { [int]$thByFips[$fp] } else { 0 }
        $tot = if ($fipsZipTotal.ContainsKey($fp)) { [int]$fipsZipTotal[$fp] } else { 0 }
        $cov = if ($tot -gt 0) { [math]::Round(100.0 * $sweptByFips[$fp] / $tot, 0) } else { 0 }
        $rowList.Add([pscustomobject]@{
            County = if ($mkt) { [string]$mkt.County } else { "FIPS $fp" }
            State = if ($mkt) { [string]$mkt.State } else { '' }
            CountyFips = $fp
            FfsBeneficiaries = if ($mkt) { $ffs } else { '' }
            TherapistsInSweep = $th
            TherapistsPer10kFfs = if ($mkt -and $ffs -gt 0) { [math]::Round($th / ($ffs / 10000.0), 1) } else { '' }
            ZipsSwept = [int]$sweptByFips[$fp]
            ZipsInCounty = $tot
            CoveragePct = $cov
        })
    }
    # Most underserved first, but only fully-measured counties can rank:
    # rows without a market figure sink to the bottom.
    $rows = @($rowList.ToArray() | Sort-Object -Property `
        @{Expression = { if ($_.TherapistsPer10kFfs -is [double]) { 0 } else { 1 } }},
        @{Expression = { if ($_.TherapistsPer10kFfs -is [double]) { [double]$_.TherapistsPer10kFfs } else { 0 } }},
        @{Expression = 'County'})

    [pscustomobject]@{
        CenterZip = $Zip
        RadiusMiles = $RadiusMiles
        ZipCount = $zips.Count
        TherapistTotal = $thTotal
        Rows = $rows
        Notes = @(
            "UNDERSERVED-AREA SCREEN: counties touched by the $RadiusMiles-mile sweep around ZIP $Zip ($($zips.Count) ZIPs). Supply = INDIVIDUAL clinicians with any PT/OT/SLP taxonomy registered in a swept ZIP (NPPES practice address - where they are registered, not necessarily every site they treat at). Demand = the county's Original-Medicare (FFS) beneficiaries, CMS Medicare Monthly Enrollment, latest full year."
            'TherapistsPer10kFfs = clinicians per 10,000 FFS beneficiaries. LOWER = fewer clinicians per Medicare patient = more open demand. Only compare counties with HIGH CoveragePct: a partially swept county counts only part of its clinicians against ALL of its beneficiaries, which understates supply.'
            'Medicare Advantage members are NOT in the denominator - in high-MA markets total senior demand is larger than the FFS figure suggests.'
            'Cash-pay demand, clinician caseloads, and part-time status are invisible here: treat this as a screening ranking, not a market study.'
            $(if ($noMarket -gt 0) { "$noMarket county(ies) had no enrollment figure (no local enrollment index and the CMS API was unreachable) - their rate is blank." })
        ) | Where-Object { $_ }
    }
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

function Get-RmSourceAnalysis {
    <#
    .SYNOPSIS
      In-depth referral-source analysis for ONE organization/provider NPI on
      the active dataset: every inbound source ranked with share and
      cumulative share, geography (distance from the practice), specialty
      mix, concentration metrics (top-1/top-5 dependence, HHI), and — on
      CareSet data — a referral-lag profile. Also sweeps the practice's
      competitive landscape: every outpatient rehab provider within
      -CompetitorRadiusMiles straight-line miles, ranked by inbound volume,
      with the practice's own rank and share. Feed the result to
      Export-RmSourceReportHtml for the client-ready report.
    .PARAMETER CompetitorRadiusMiles
      Radius for the competitive sweep (default 10). -SkipCompetitors skips
      the sweep entirely (faster; Competitive comes back $null).
    #>
    [CmdletBinding()]
    param(
        # One NPI, or SEVERAL to analyze as one combined practice (org NPI +
        # therapist NPIs) — the answer to volume being split across NPIs,
        # which hits small practices hardest. The FIRST NPI is the primary:
        # it names the practice and centers the geography.
        [Parameter(Mandatory)][ValidateCount(1, 50)][ValidatePattern('^\d{10}$')][string[]]$Npi,
        [ValidateRange(1, 100)][double]$CompetitorRadiusMiles = 10,
        [switch]$SkipCompetitors,
        [string]$CentroidPath,
        [string]$CrosswalkPath,    # test override for the ZIP->county table
        # Path to a current Order & Referring snapshot CSV. When given,
        # referrer-type sources (MD/DO, NP, PA, ...) are cross-checked
        # against it and vanished referrers are flagged as at-risk.
        [string]$EligibilityIndexPath
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $isHop = $info.Source -eq 'hop-teaming'
    $centroids = Get-RmCentroids $CentroidPath

    # Dedupe, keeping the caller's order (first stays primary).
    $memberSet = New-Object 'System.Collections.Generic.HashSet[string]'
    $npiList = @($Npi | Where-Object { $memberSet.Add($_) })
    $primary = $npiList[0]

    # The practice itself (the primary NPI).
    $resp = Invoke-RmNppes ('number={0}' -f $primary)
    $hits = @(Get-RmProp $resp 'results')
    if ($hits.Count -eq 0) { throw "NPI $primary was not found in the NPPES registry (deactivated, or a typo)." }
    $r0 = $hits[0]
    $basic = Get-RmProp $r0 'basic'
    $isOrg = ((Get-RmProp $r0 'enumeration_type') -eq 'NPI-2')
    $pracName = if ($isOrg) { [string](Get-RmProp $basic 'organization_name') }
                else { ('{0} {1}' -f (Get-RmProp $basic 'first_name'), (Get-RmProp $basic 'last_name')).Trim() }
    $loc = @(@(Get-RmProp $r0 'addresses') | Where-Object { (Get-RmProp $_ 'address_purpose') -eq 'LOCATION' })
    $postal = if ($loc.Count) { [string](Get-RmProp $loc[0] 'postal_code') } else { '' }
    $pracZip = if ($postal.Length -ge 5) { $postal.Substring(0, 5) } else { '' }
    $pracLoc = if ($pracZip -and $centroids.ContainsKey($pracZip)) { $centroids[$pracZip] } else { $null }

    # Discover the competitor set BEFORE scanning: it depends only on the
    # practice's NPPES ZIP, so knowing it up front lets ONE pass feed the
    # analysis, the outbound view and the competitive layer. A failure here
    # must not sink the analysis, exactly as when the sweep ran later.
    $peersAll = @(); $peers = @(); $secondaryOnly = 0
    $others = New-Object 'System.Collections.Generic.HashSet[string]'
    $rzips = @(); $sweepError = $null
    $outboundRows = @()   # NOT $outRows: that name is the competitive peer list below
    if (-not $SkipCompetitors) {
        try {
            if ($pracZip -notmatch '^\d{5}$') {
                throw "NPPES lists no usable 5-digit practice-location ZIP for $Npi, so the radius cannot be centered."
            }
            $rzips = @(Get-RmZipsInRadius -Zip $pracZip -RadiusMiles $CompetitorRadiusMiles -CentroidPath $CentroidPath)
            Write-Verbose "Competitive sweep: $($rzips.Count) ZIP(s) within $CompetitorRadiusMiles mi of $pracZip..."
            $peersAll = @(Find-RmClinic -ZipList $rzips)
            # Rank only COMPARABLE therapy practices: a provider whose
            # PRIMARY taxonomy is in scope. Hospitals and multi-specialty
            # organizations that merely list a therapy taxonomy in a spare
            # slot are real, but their inbound volume covers every service
            # line — including one live put a 319,024-patient hospital at
            # "#1 therapy provider" and halved this practice's apparent
            # share. They are counted and disclosed, not ranked.
            $peers = @($peersAll | Where-Object {
                $null -eq $_.PSObject.Properties['PrimaryInScope'] -or $_.PrimaryInScope })
            $secondaryOnly = @($peersAll).Count - @($peers).Count
            foreach ($p in $peers) { if (-not $memberSet.Contains($p.NPI)) { [void]$others.Add($p.NPI) } }
        } catch {
            $sweepError = $_.Exception.Message
            Write-Warning "COMPETITIVE LANDSCAPE unavailable: $sweepError"
            $others = New-Object 'System.Collections.Generic.HashSet[string]'
        }
    }

    Write-Verbose "Scanning $($info.Label): $($npiList.Count) member NPI(s) + $($others.Count) competitor(s) in ONE pass..."
    # ONE engine pass now serves three consumers: inbound pairs for the
    # analysis, outbound pairs for the destinations view, and competitor
    # inbound for ranking and market capture — the same 8 GB used to be read
    # three times. Then (a) drop flows BETWEEN members — internal handoffs
    # are not external referrals — and (b) merge a source feeding several
    # members into ONE row with summed volume (AvgDayWait becomes the
    # transaction-weighted pooled mean). For a single NPI both steps are no-ops.
    $allEdges = @([RmEngine]::ScanCombined($info.Path, $memberSet, $others, $info.Format))
    $rawEdges = New-Object System.Collections.Generic.List[object]
    $peerEdges = New-Object System.Collections.Generic.List[object]
    $outEdges = New-Object System.Collections.Generic.List[object]
    foreach ($e in $allEdges) {
        if ($memberSet.Contains($e.TargetNpi)) { $rawEdges.Add($e) }
        elseif ($others.Contains($e.TargetNpi)) { $peerEdges.Add($e) }
        if ($memberSet.Contains($e.SourceNpi) -and -not $memberSet.Contains($e.TargetNpi)) { $outEdges.Add($e) }
    }
    # Outbound destinations, merged across member NPIs, so the one-stop report
    # no longer needs its own pass for them. average_day_wait in the file is a
    # per-TRANSACTION mean, so pooling weights by transaction_count (PairCount)
    # - that exactly reconstructs the mean over all underlying events, where
    # patient-weighting would drift whenever one patient bounces repeatedly.
    $outBySrc = @{}
    foreach ($e in $outEdges) {
        if (-not $outBySrc.ContainsKey($e.TargetNpi)) {
            $outBySrc[$e.TargetNpi] = [pscustomobject]@{ N = 0; W = [double]0; T = [long]0 }
        }
        $outBySrc[$e.TargetNpi].N += $e.BeneCount
        $outBySrc[$e.TargetNpi].W += ([double]$e.AvgDayWait * $e.PairCount)
        $outBySrc[$e.TargetNpi].T += $e.PairCount
    }
    $mergedBySrc = @{}
    foreach ($e in $rawEdges) {
        if ($memberSet.Contains($e.SourceNpi)) { continue }
        if (-not $mergedBySrc.ContainsKey($e.SourceNpi)) {
            $mergedBySrc[$e.SourceNpi] = [pscustomobject]@{
                SourceNpi = $e.SourceNpi; BeneCount = 0; PairCount = 0
                AvgDayWait = [double]0; _WaitWeighted = [double]0
            }
        }
        $m = $mergedBySrc[$e.SourceNpi]
        $m.BeneCount += $e.BeneCount
        $m.PairCount += $e.PairCount
        if ($isHop) { $m._WaitWeighted += [double]$e.AvgDayWait * $e.PairCount }
    }
    foreach ($m in $mergedBySrc.Values) {
        if ($isHop -and $m.PairCount -gt 0) { $m.AvgDayWait = [math]::Round($m._WaitWeighted / $m.PairCount, 1) }
    }
    $edges = @($mergedBySrc.Values |
        Sort-Object -Property @{Expression = 'BeneCount'; Descending = $true},
                              @{Expression = 'SourceNpi'; Descending = $false})
    $total = 0; foreach ($e in $edges) { $total += $e.BeneCount }

    # Locate/name every source (cached; volume-capped like the heat map).
    $srcNpis = @($edges | ForEach-Object { $_.SourceNpi })
    $capNote = $null
    if ($srcNpis.Count -gt $script:RmConfig.EnrichCap) {
        $capNote = ("Named/located the top {0} of {1} sources by volume; the rest show NPI only. " +
            "Raise with Set-RmConfig -EnrichCap.") -f $script:RmConfig.EnrichCap, $srcNpis.Count
        Write-Warning $capNote
        $srcNpis = @($srcNpis | Select-Object -First $script:RmConfig.EnrichCap)
    }
    $detail = if ($srcNpis.Count -gt 0) { Get-RmProviderDetail -Npi $srcNpis -RequireZip } else { @{} }

    # --- Practice-therapist reclassification -------------------------------
    # An organization's own PT/OT/SLPs bill under their INDIVIDUAL NPIs, so
    # the raw file lists them among the org's biggest "sources". That volume
    # is the practice's own patient base arriving through its clinicians —
    # internal care like the member-to-member exclusion above, not a referral
    # anyone could win or lose. Every individual-therapist source is
    # therefore moved out of the referral ranking into a separate
    # practice-therapist roll-up (shown, never hidden), and Care Compare's
    # roster — when the local index is present — marks which of them are
    # VERIFIED staff of this practice. Sources beyond the enrichment cap
    # carry no specialty and stay in the referral list (the cap note
    # discloses that). Clinic/Center taxonomies never match: a therapy ORG
    # appearing as a source is a competitor relationship, still listed.
    $therapistRe = '(PHYSICAL|OCCUPATIONAL) THERAPIST|SPEECH.LANGUAGE PATHOLOGIST'
    $therEdges = New-Object System.Collections.Generic.List[object]
    $keepEdges = New-Object System.Collections.Generic.List[object]
    foreach ($e in $edges) {
        $d0 = if ($detail.ContainsKey($e.SourceNpi)) { $detail[$e.SourceNpi] } else { $null }
        $spec0 = if ($d0) { ([string]$d0.Specialty).ToUpperInvariant() } else { '' }
        if ($spec0 -and $spec0 -notmatch 'CLINIC|CENTER' -and $spec0 -match $therapistRe) {
            $therEdges.Add($e)
        } else {
            $keepEdges.Add($e)
        }
    }
    # The practice's Care Compare roster, fetched whenever the local DAC
    # index exists (not only when inbound therapist rows need labels): it
    # (a) marks which therapist "sources" are VERIFIED current staff,
    # (b) folds own-clinician rows out of the OUTBOUND destinations below,
    # and (c) is returned as PracticeRoster so the report can display who
    # bills under this practice today. Name-matched rows seed the practice's
    # group-enrollment id(s) (org_pac_id); a second pass pulls every
    # clinician sharing those ids, so staff enrolled under a variant
    # facility-name spelling still count.
    $staffSet = New-Object 'System.Collections.Generic.HashSet[string]'
    $rosterRows = New-Object System.Collections.Generic.List[object]
    try {
        $dacIdx = Get-RmDacIndexPath
        $rosterNeedle = Get-RmOrgNameKey $pracName
        if ($rosterNeedle -and (Test-Path -LiteralPath $dacIdx)) {
            $m0 = Get-RmNameMatcher $rosterNeedle
            $rosterPacs = New-Object 'System.Collections.Generic.HashSet[string]'
            foreach ($line in @([RmEngine]::FindByOrgName($dacIdx, -1, 2, $m0.Regex.ToString(), $m0.Compact, $null))) {
                $f = $line.Split('|')
                if ($f.Count -ge 8 -and $f[0]) {
                    if ($f[1]) { [void]$rosterPacs.Add($f[1]) }
                    if ($staffSet.Add($f[0])) {
                        $rosterRows.Add([pscustomobject]@{
                            NPI = $f[0]; Clinician = ("$($f[5]) $($f[4])").Trim()
                            Specialty = $f[3]; City = $f[6]; State = $f[7]
                        })
                    }
                }
            }
            if ($rosterPacs.Count -gt 0) {
                foreach ($line in @([RmEngine]::ScanRosterIndex($dacIdx, $rosterPacs, 1))) {
                    $f = $line.Split('|')
                    if ($f.Count -ge 8 -and $f[0] -and $staffSet.Add($f[0])) {
                        $rosterRows.Add([pscustomobject]@{
                            NPI = $f[0]; Clinician = ("$($f[5]) $($f[4])").Trim()
                            Specialty = $f[3]; City = $f[6]; State = $f[7]
                        })
                    }
                }
            }
        }
    } catch { }   # the roster only labels rows; never sink the analysis
    $grandTotal = $total
    # .ToArray(), not @($list) — the @() binder can throw a spurious
    # 'Argument types do not match' on a generic List here (see the trend).
    $edges = $keepEdges.ToArray()
    $total = 0; foreach ($e in $edges) { $total += $e.BeneCount }
    $therTotal = $grandTotal - $total
    $therRows = New-Object System.Collections.Generic.List[object]
    foreach ($e in $therEdges) {
        $d0 = if ($detail.ContainsKey($e.SourceNpi)) { $detail[$e.SourceNpi] } else { $null }
        $trow = [ordered]@{
            SourceNPI       = $e.SourceNpi
            SourceName      = if ($d0) { [string]$d0.Name } else { '' }
            SourceSpecialty = if ($d0) { [string]$d0.Specialty } else { '' }
            SharedPatients  = $e.BeneCount
            Staff           = if ($staffSet.Contains($e.SourceNpi)) { 'VERIFIED (Care Compare)' } else { 'same discipline' }
        }
        if ($isHop) { $trow['AvgDayWait'] = $e.AvgDayWait }
        $therRows.Add([pscustomobject]$trow)
    }

    # Outbound rows, named from the top destinations only (the same cached
    # lookup the sources use, so a handful of extra NPIs at most). The
    # practice's own PT/OT/SLPs appear on THIS side too — the co-billing
    # pattern in reverse (org visit first, therapist billed later) — so the
    # same fold applies: an individual-therapist destination moves to a
    # separate own-clinician list instead of masquerading as a hand-off.
    # A roster-NPI match catches every CURRENT staff member at any volume;
    # the specialty test catches departed staff among the top destinations.
    $outboundTherRows = @()
    if ($outBySrc.Count -gt 0) {
        # Classify a deeper window than the 25 shown, so folding staff out
        # never leaves the external list short.
        $topOut = @($outBySrc.GetEnumerator() | Sort-Object { $_.Value.N } -Descending | Select-Object -First 60)
        $outNpis = @($topOut | ForEach-Object { $_.Key } | Where-Object { -not $detail.ContainsKey($_) })
        $outDetail = if ($outNpis.Count -gt 0) { Get-RmProviderDetail -Npi $outNpis } else { @{} }
        $outExt = New-Object System.Collections.Generic.List[object]
        $outTher = New-Object System.Collections.Generic.List[object]
        foreach ($t in $topOut) {
            $d = if ($detail.ContainsKey($t.Key)) { $detail[$t.Key] }
                 elseif ($outDetail.ContainsKey($t.Key)) { $outDetail[$t.Key] } else { $null }
            $spec0 = if ($d) { ([string]$d.Specialty).ToUpperInvariant() } else { '' }
            $isOwn = $staffSet.Contains($t.Key) -or
                     ($spec0 -and $spec0 -notmatch 'CLINIC|CENTER' -and $spec0 -match $therapistRe)
            $row = [ordered]@{
                NPI = $t.Key
                Name = if ($d) { [string]$d.Name } else { '' }
                Specialty = if ($d) { [string]$d.Specialty } else { '' }
                SharedPatients = $t.Value.N
            }
            if ($isHop -and $t.Value.T -gt 0) { $row['AvgDayWait'] = [math]::Round($t.Value.W / $t.Value.T, 1) }
            if ($isOwn) {
                $row['Staff'] = if ($staffSet.Contains($t.Key)) { 'VERIFIED (Care Compare)' } else { 'same discipline' }
                $outTher.Add([pscustomobject]$row)
            } else {
                $outExt.Add([pscustomobject]$row)
            }
        }
        $outboundRows = @($outExt.ToArray() | Select-Object -First 25)
        $outboundTherRows = $outTher.ToArray()
    }
    $outTherPatients = 0
    foreach ($o in $outboundTherRows) { $outTherPatients += [int]$o.SharedPatients }

    # Departed-staff context: a "same discipline" clinician is usually an
    # ex-employee, and where they bill TODAY changes what the row means -
    # on the live Boulder case the biggest folded row (143 patients) now
    # runs his own practice one town over: a competitor, not history. One
    # roster scan labels every such row on both sides.
    $sdNpis = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($r0 in $therRows) { if ([string]$r0.Staff -eq 'same discipline') { [void]$sdNpis.Add([string]$r0.SourceNPI) } }
    foreach ($r0 in $outboundTherRows) { if ([string]$r0.Staff -eq 'same discipline') { [void]$sdNpis.Add([string]$r0.NPI) } }
    if ($sdNpis.Count -gt 0) {
        try {
            $dacIdx2 = Get-RmDacIndexPath
            if (Test-Path -LiteralPath $dacIdx2) {
                $nowAt = @{}
                foreach ($line in @([RmEngine]::ScanRosterIndex($dacIdx2, $sdNpis, 0))) {
                    $f = $line.Split('|')
                    if ($f.Count -lt 8 -or -not $f[2]) { continue }
                    if (-not $nowAt.ContainsKey($f[0])) { $nowAt[$f[0]] = New-Object System.Collections.Generic.List[string] }
                    $tag = "$($f[2]) ($($f[6]), $($f[7]))"
                    if (-not $nowAt[$f[0]].Contains($tag)) { $nowAt[$f[0]].Add($tag) }
                }
                foreach ($r0 in $therRows) {
                    if ([string]$r0.Staff -eq 'same discipline' -and $nowAt.ContainsKey([string]$r0.SourceNPI)) {
                        $tags = $nowAt[[string]$r0.SourceNPI]
                        $r0.Staff = 'same discipline - now at ' + (@($tags | Select-Object -First 2) -join '; ') +
                            $(if ($tags.Count -gt 2) { "; +$($tags.Count - 2) more" })
                    }
                }
                foreach ($r0 in $outboundTherRows) {
                    if ([string]$r0.Staff -eq 'same discipline' -and $nowAt.ContainsKey([string]$r0.NPI)) {
                        $tags = $nowAt[[string]$r0.NPI]
                        $r0.Staff = 'same discipline - now at ' + (@($tags | Select-Object -First 2) -join '; ') +
                            $(if ($tags.Count -gt 2) { "; +$($tags.Count - 2) more" })
                    }
                }
            }
        } catch { }   # a label, never worth failing the analysis over
    }

    # Per-source ranked rows with share, cumulative share, and distance —
    # and, in the same pass, the per-ZIP geography roll-up the report's
    # embedded heat map draws (no extra scan; edges are volume-sorted, so
    # the first source seen in a ZIP is that ZIP's top source).
    # Net flow per source: the same pass carried outbound, so each source row
    # can say how many patients went BACK to that provider. Live Boulder
    # check: ALL top-25 sources are bidirectional - in≈back with a long lag
    # is co-occurring care (labs, pharmacies), in >> back with a short lag is
    # referral flow someone could win or lose.
    $outByNpi = @{}
    foreach ($kv in $outBySrc.GetEnumerator()) { $outByNpi[$kv.Key] = [int]$kv.Value.N }
    $rank = 0; $cum = 0.0
    $srcRows = New-Object System.Collections.Generic.List[object]
    $byZip = @{}
    $geoUnmapped = 0
    $geoMappedNpis = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($e in $edges) {
        $rank++
        $cum += $e.BeneCount
        $d = if ($detail.ContainsKey($e.SourceNpi)) { $detail[$e.SourceNpi] } else { $null }
        $zip = if ($d -and $null -ne $d.PSObject.Properties['Zip'] -and ([string]$d.Zip) -match '^\d{5}$') { [string]$d.Zip } else { '' }
        $dist = if ($zip -and $pracLoc -and $centroids.ContainsKey($zip)) {
            Get-RmMilesBetween $pracLoc[0] $pracLoc[1] $centroids[$zip][0] $centroids[$zip][1]
        } else { $null }
        if ($zip -and $centroids.ContainsKey($zip)) {
            [void]$geoMappedNpis.Add($e.SourceNpi)
            if (-not $byZip.ContainsKey($zip)) {
                $byZip[$zip] = [pscustomobject]@{
                    City = $(if ($d) { [string]$d.City } else { '' })
                    State = $(if ($d) { [string]$d.State } else { '' })
                    Patients = 0; Sources = 0
                    Top = $(if ($d -and $d.Name) { [string]$d.Name } else { $e.SourceNpi })
                    Dist = $dist
                    Members = (New-Object System.Collections.Generic.List[object])
                }
            }
            $byZip[$zip].Patients += $e.BeneCount
            $byZip[$zip].Sources += 1
            # Keep the top few actual providers per ZIP: clicking a circle
            # should name the outreach targets, not just a count.
            if ($byZip[$zip].Members.Count -lt 6) {
                $byZip[$zip].Members.Add([pscustomobject]@{
                    Name = $(if ($d -and $d.Name) { [string]$d.Name } else { "NPI $($e.SourceNpi)" })
                    Specialty = $(if ($d) { [string]$d.Specialty } else { '' })
                    Patients = $e.BeneCount
                })
            }
        } else {
            $geoUnmapped += $e.BeneCount
        }
        $row = [ordered]@{
            Rank            = $rank
            SourceNPI       = $e.SourceNpi
            SourceName      = if ($d) { $d.Name } else { '' }
            SourceSpecialty = if ($d) { $d.Specialty } else { '' }
            City            = if ($d) { $d.City } else { '' }
            State           = if ($d) { $d.State } else { '' }
            SharedPatients  = $e.BeneCount
            SharedBack      = if ($outByNpi.ContainsKey($e.SourceNpi)) { $outByNpi[$e.SourceNpi] } else { 0 }
            PctOfVolume     = if ($total -gt 0) { [math]::Round(100.0 * $e.BeneCount / $total, 1) } else { 0 }
            CumulativePct   = if ($total -gt 0) { [math]::Round(100.0 * $cum / $total, 1) } else { 0 }
            DistanceMiles   = if ($null -ne $dist) { $dist } else { '' }
            Eligibility     = ''   # filled by the O&R cross-check below when a snapshot is given
        }
        if ($isHop) { $row['AvgDayWait'] = $e.AvgDayWait }
        $srcRows.Add([pscustomobject]$row)
    }
    $sources = $srcRows.ToArray()

    # Eligibility cross-check (optional): the Order & Referring roster is the
    # app's ORIGINAL dataset, and a referrer who vanished from it has
    # retired, deactivated, or left Medicare - future Medicare referrals
    # from them would deny. Only REFERRER-TYPE sources are checked (the
    # provider types the roster covers: MD/DO, NP, PA, podiatry, optometry,
    # dental, chiropractic, CNS). Individual PT/OT/SLPs are never flagged -
    # therapists are not order/refer-eligible, so absence is normal - and
    # organizations are never on the roster at all.
    $atRisk = @()
    if ($EligibilityIndexPath -and (Test-Path -LiteralPath $EligibilityIndexPath)) {
        try {
            # Local-index enrichment labels physicians '... Physician' (NUCC
            # display names); the NPPES API returns the bare taxonomy desc
            # ('Family Medicine'). Cover both so the check does not silently
            # skip API-enriched sources.
            $refTypeRe = ('PHYSICIAN|NURSE PRACTITIONER|PHYSICIAN ASSISTANT|PODIATR|OPTOMETR|DENTIST|CHIROPRACT|' +
                'CLINICAL NURSE SPECIALIST|CERTIFIED REGISTERED NURSE ANESTHETIST|CERTIFIED NURSE MIDWIFE|' +
                'FAMILY MEDICINE|INTERNAL MEDICINE|GENERAL PRACTICE|ORTHOPAEDIC|NEUROLOG|CARDIOVASCULAR|SPORTS MEDICINE|' +
                'PHYSICAL MEDICINE|OSTEOPATH|SURGERY|PSYCHIATR|RHEUMATOLOG|PAIN MEDICINE|GERIATRIC|PULMONARY|ENDOCRIN|' +
                'GASTROENTER|OTOLARYNG|UROLOG|ONCOLOG|RADIOLOG|ANESTHESIOLOG|DERMATOLOG|EMERGENCY MEDICINE|OBSTETRIC|' +
                'GYNECOLOG|OPHTHALMOLOG|NEPHROLOG|HEMATOLOG|INFECTIOUS|ALLERGY')
            $checkSet = New-Object 'System.Collections.Generic.HashSet[string]'
            foreach ($srow in $sources) {
                $sp = ([string]$srow.SourceSpecialty).ToUpperInvariant()
                if ($sp -and $sp -match $refTypeRe -and $sp -notmatch 'CLINIC|CENTER|HOSPITAL') {
                    [void]$checkSet.Add([string]$srow.SourceNPI)
                }
            }
            # INDIVIDUALS only: an organization NPI carries physician-style
            # labels ('Diagnostic Radiology Physician') but is never on the
            # O&R roster - flagging one is a false alarm (the live Boulder
            # run surfaced 72 of them before this filter). RequireEntity
            # upgrades old cache entries in place; unknown entity = never
            # flagged.
            if ($checkSet.Count -gt 0) {
                $entDetail = Get-RmProviderDetail -Npi @($checkSet) -RequireEntity
                foreach ($ck in @($checkSet)) {
                    $ed = if ($entDetail.ContainsKey($ck)) { $entDetail[$ck] } else { $null }
                    $ent = if ($ed -and $null -ne $ed.PSObject.Properties['Entity']) { [string]$ed.Entity } else { '' }
                    if ($ent -ne '1') { [void]$checkSet.Remove($ck) }
                }
            }
            if ($checkSet.Count -gt 0) {
                # PARTB is the 5th field from the END (NPI,LAST,FIRST,PARTB,
                # DME,HHA,PMD,HOSPICE): counting from the end stays correct
                # even if a quoted name carries an embedded comma.
                $onList = @{}
                foreach ($line in @([RmEngine]::MatchFirstField($EligibilityIndexPath, $checkSet, ','))) {
                    $parts = $line.Split(',')
                    if ($parts.Length -lt 6) { continue }
                    $onList[$parts[0].Trim('"')] = $parts[$parts.Length - 5].Trim('"')
                }
                $atRiskList = New-Object System.Collections.Generic.List[object]
                foreach ($srow in $sources) {
                    if (-not $checkSet.Contains([string]$srow.SourceNPI)) { continue }
                    $status = if (-not $onList.ContainsKey([string]$srow.SourceNPI)) {
                                  'NOT on the current Order & Referring list'
                              } elseif ($onList[[string]$srow.SourceNPI] -ne 'Y') {
                                  'on the list, but NOT Part B eligible'
                              } else { 'eligible' }
                    $srow.Eligibility = $status
                    if ($status -ne 'eligible') {
                        $atRiskList.Add([pscustomobject]@{
                            SourceNPI = $srow.SourceNPI; SourceName = $srow.SourceName
                            SourceSpecialty = $srow.SourceSpecialty
                            SharedPatients = $srow.SharedPatients; Status = $status
                        })
                    }
                }
                $atRisk = $atRiskList.ToArray()
            }
        } catch { }   # a cross-check label; never sink the analysis
    }
    $geoRows = @($byZip.GetEnumerator() | ForEach-Object {
        $z = $_.Key; $b = $_.Value
        [pscustomobject]@{
            Zip = $z; City = $b.City; State = $b.State
            Sources = $b.Sources; SharedPatients = $b.Patients
            PctOfVolume = if ($total -gt 0) { [math]::Round(100.0 * $b.Patients / $total, 1) } else { 0 }
            DistanceMiles = if ($null -ne $b.Dist) { $b.Dist } else { '' }
            Lat = $centroids[$z][0]; Lon = $centroids[$z][1]
            TopSource = $b.Top
            TopProviders = @($b.Members.ToArray())
        }
    } | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                              @{Expression = 'Zip'; Descending = $false})

    # DISTANT SOURCES. A real user asked why a Carmichael CA practice showed
    # circles in Berkeley, Cleveland and New York. The pairs are real, but
    # the DOT IS PLOTTED AT THE SOURCE'S REGISTERED ADDRESS, and centralized
    # services register centrally: on that practice, 88.4% of mapped volume
    # sat within 40 miles and 78% of the remainder was reference labs
    # (Quest Nichols Institute, LabCorp, Cleveland HeartLab, Exact
    # Sciences), teleradiology reads and a pharmacy chain's corporate NPI.
    # Those are co-occurring care, not distant referrers - so the map says
    # so instead of leaving the reader to guess.
    $remoteSpecialties = @('LABORATOR', 'PATHOLOG', 'RADIOLOG', 'PHARMAC', 'DURABLE MEDICAL',
                           'PROSTHETIC', 'ORTHOTIC', 'SUPPLIER', 'AMBULANCE', 'TELEHEALTH')
    $farCut = 40.0
    $farVol = 0; $farRemote = 0; $farZips = 0
    $farBySpec = @{}
    foreach ($gr in $geoRows) {
        if ($gr.DistanceMiles -isnot [double] -or $gr.DistanceMiles -le $farCut) { continue }
        $farZips++; $farVol += [int]$gr.SharedPatients
    }
    foreach ($e in $edges) {
        $d = $detail[$e.SourceNpi]
        if (-not $d) { continue }
        $z = [string]$d.Zip
        if ($z.Length -lt 5 -or -not $centroids.ContainsKey($z) -or -not $pracLoc) { continue }
        $dist = Get-RmMilesBetween $pracLoc[0] $pracLoc[1] $centroids[$z][0] $centroids[$z][1]
        if ($dist -le $farCut) { continue }
        $spec = ([string]$d.Specialty).ToUpperInvariant()
        $isRemote = $false
        foreach ($rs in $remoteSpecialties) { if ($spec.Contains($rs)) { $isRemote = $true; break } }
        if ($isRemote) { $farRemote += $e.BeneCount }
        $key = if ($d.Specialty) { [string]$d.Specialty } else { '(specialty not looked up)' }
        if (-not $farBySpec.ContainsKey($key)) { $farBySpec[$key] = 0 }
        $farBySpec[$key] += $e.BeneCount
    }
    $distantNote = $null
    if ($farVol -gt 0 -and $total -gt 0) {
        $topFar = @($farBySpec.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 3 |
            ForEach-Object { "$($_.Key) ($('{0:N0}' -f $_.Value))" })
        $distantNote = ("DISTANT DOTS: $('{0:N0}' -f $farVol) patients ($([math]::Round(100.0 * $farVol / $total, 1))% of measured volume) " +
            "come from sources registered more than $([int]$farCut) miles away, across $farZips ZIP area(s)" +
            $(if ($farRemote -gt 0) { ", and $([math]::Round(100.0 * $farRemote / $farVol))% of that is centralized services" }) + ". " +
            "A source is plotted at the address its NPI is REGISTERED at, which for a reference lab, a teleradiology read, " +
            "a pharmacy chain or a DME supplier is a corporate office far from where the patient was actually seen - the " +
            "specimen was drawn locally and processed elsewhere. Biggest distant contributors: " + ($topFar -join '; ') + ". " +
            "Read these as co-occurring care, not as an out-of-area referral base; the local cluster is the outreach map.")
    }

    # Concentration metrics. HHI on shares (0-10,000): sum of squared
    # percentage shares — the antitrust-style concentration index.
    $hhi = 0.0
    foreach ($e in $edges) {
        if ($total -gt 0) { $share = 100.0 * $e.BeneCount / $total; $hhi += $share * $share }
    }
    $top1 = if ($sources.Count -ge 1) { $sources[0].PctOfVolume } else { 0 }
    # From RAW volumes, rounded once — summing the already-rounded per-row
    # percentages drifts by up to ~0.25pp (the year-over-year code always
    # did it this way; the audit found this path summing rounded shares).
    $top5v = 0; $top10v = 0; $iTop = 0
    foreach ($e in $edges) {
        $iTop++
        if ($iTop -le 5) { $top5v += $e.BeneCount }
        if ($iTop -le 10) { $top10v += $e.BeneCount } else { break }
    }
    $top5 = if ($total -gt 0) { 100.0 * $top5v / $total } else { 0 }
    $top10 = if ($total -gt 0) { 100.0 * $top10v / $total } else { 0 }
    # With no measured sources there is nothing to be concentrated OR
    # diversified — never let an empty scan read as a favorable finding.
    $concLabel = if ($srcRows.Count -eq 0) { 'n/a — no measured sources' }
                 elseif ($hhi -ge 2500) { 'HIGH — dependent on a few relationships' }
                 elseif ($hhi -ge 1500) { 'MODERATE' }
                 else { 'LOW — a diversified referral base' }

    # Distance bands (volume-weighted).
    $bandDefs = @(
        @{ Label = '0-5 mi'; Min = 0.0; Max = 5.0 }
        @{ Label = '5-10 mi'; Min = 5.0; Max = 10.0 }
        @{ Label = '10-25 mi'; Min = 10.0; Max = 25.0 }
        @{ Label = '25-50 mi'; Min = 25.0; Max = 50.0 }
        @{ Label = '50+ mi'; Min = 50.0; Max = [double]::MaxValue }
    )
    $distBands = foreach ($b in $bandDefs) {
        $vol = 0
        foreach ($srcRow in $sources) {
            # type check, NOT -ne '': PowerShell coerces '' to 0 next to a
            # number, so a legitimate 0.0-mile distance would look 'unknown'.
            if ($srcRow.DistanceMiles -is [double]) {
                $dv = [double]$srcRow.DistanceMiles
                if ($dv -ge $b.Min -and $dv -lt $b.Max) { $vol += $srcRow.SharedPatients }
            }
        }
        [pscustomobject]@{ Band = $b.Label; SharedPatients = $vol
                           Pct = if ($total -gt 0) { [math]::Round(100.0 * $vol / $total, 1) } else { 0 } }
    }
    $unknownDist = 0
    foreach ($srcRow in $sources) {
        if ($srcRow.DistanceMiles -isnot [double]) { $unknownDist += $srcRow.SharedPatients }
    }
    $distBands = @($distBands) + @([pscustomobject]@{ Band = 'Not locatable'; SharedPatients = $unknownDist
        Pct = if ($total -gt 0) { [math]::Round(100.0 * $unknownDist / $total, 1) } else { 0 } })

    # Referral-lag profile (hop only, volume-weighted by patient count).
    $waitBands = @()
    if ($isHop) {
        $waitDefs = @(
            @{ Label = '0-7 days'; Min = 0.0; Max = 7.0; Read = 'tight referral loop' }
            @{ Label = '7-30 days'; Min = 7.0; Max = 30.0; Read = 'typical referral window' }
            @{ Label = '30-90 days'; Min = 30.0; Max = 90.0; Read = 'loose / episodic' }
            @{ Label = '90+ days'; Min = 90.0; Max = [double]::MaxValue; Read = 'likely co-occurring care' }
        )
        $waitBands = foreach ($b in $waitDefs) {
            $vol = 0
            foreach ($e in $edges) {
                if ($e.AvgDayWait -ge $b.Min -and $e.AvgDayWait -lt $b.Max) { $vol += $e.BeneCount }
            }
            [pscustomobject]@{ Band = $b.Label; Reading = $b.Read; SharedPatients = $vol
                               Pct = if ($total -gt 0) { [math]::Round(100.0 * $vol / $total, 1) } else { 0 } }
        }
    }

    $mix = if ($sources.Count) { @(Get-RmSourceSpecialtyMix -Rows $sources) } else { @() }

    # Competitive landscape: the same NPPES outpatient-rehab taxonomy sweep
    # the Referral map uses, over every ZIP whose centroid falls within the
    # radius, then ONE engine pass for the peers' inbound volumes. The
    # analyzed practice's own row reuses the volumes computed above — the
    # three code paths must agree by construction. A sweep failure degrades
    # to a note; it never kills the main analysis.
    $landscape = $null
    $landscapeNote = $null
    $geoMarket = @()
    $missedRows = @()
    if (-not $SkipCompetitors) {
        try {
            if ($sweepError) { throw $sweepError }
            $peerAgg = @{}
            $areaBySource = @{}   # source NPI -> patients sent to COMPETITORS
            if ($others.Count -gt 0) {
                # Already read above, in the same pass as the practice.
                foreach ($e in $peerEdges) {
                    # Market-capture layer: how much therapy volume each
                    # source sends to the AREA (competitors), so the map can
                    # show the practice's capture rate per source ZIP.
                    # The practice's OWN onward flow to a competitor is not a
                    # source opportunity - counting it inflated the home
                    # ZIP's area volume (caught by independent recompute).
                    # It still counts toward that competitor's inbound total.
                    if (-not $memberSet.Contains($e.SourceNpi)) {
                        if ($areaBySource.ContainsKey($e.SourceNpi)) { $areaBySource[$e.SourceNpi] += $e.BeneCount }
                        else { $areaBySource[$e.SourceNpi] = $e.BeneCount }
                    }
                    if (-not $peerAgg.ContainsKey($e.TargetNpi)) {
                        $peerAgg[$e.TargetNpi] = [pscustomobject]@{ Benes = 0; Sources = 0 }
                    }
                    $peerAgg[$e.TargetNpi].Benes += $e.BeneCount
                    $peerAgg[$e.TargetNpi].Sources += 1
                }
            }
            # The analyzed practice always gets a row, built from its OWN scan
            # above (even when its taxonomy is outside the rehab sweep).
            # RAW inbound, incl. practice-therapist volume: peer volumes are
            # raw org-level inbound (with THEIR staff in it), so the ranking
            # must compare like with like.
            $rows = New-Object System.Collections.Generic.List[object]
            $rows.Add([pscustomobject]@{
                NPI = $primary; Name = $pracName
                Type = if ($isOrg) { 'Organization' } else { 'Individual' }
                City = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
                State = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
                Zip = $pracZip; DistanceMiles = [double]0
                ReferralSources = ($edges.Count + $therEdges.Count); SharedPatients = $grandTotal
            })
            foreach ($p in $peers) {
                if ($memberSet.Contains($p.NPI)) { continue }
                $agg = if ($peerAgg.ContainsKey($p.NPI)) { $peerAgg[$p.NPI] } else { $null }
                $pz = [string]$p.Zip
                $rows.Add([pscustomobject]@{
                    NPI = $p.NPI; Name = $p.Name; Type = $p.Type
                    City = $p.City; State = $p.State; Zip = $pz
                    DistanceMiles = $(
                        $dmin = Get-RmClosestSiteDistance -Npi $p.NPI -RowZip $pz -RefLoc $pracLoc -Cents $centroids
                        if ($null -ne $dmin) { $dmin } else { '' })
                    ReferralSources = $(if ($agg) { $agg.Sources } else { 0 })
                    SharedPatients  = $(if ($agg) { $agg.Benes } else { 0 })
                })
            }
            $regionTotal = 0; foreach ($rw in $rows) { $regionTotal += [int]$rw.SharedPatients }
            $rankedPeers = @($rows.ToArray() |
                Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                      @{Expression = 'Name'; Descending = $false})
            # Competition ("1224") ranking: equal volumes share a rank, so a
            # small practice with zero measured volume gets the honest tie
            # rank instead of an arbitrary alphabetical position.
            $myRank = 0; $withVol = 0
            $curRank = 0; $prevVol = -1
            $outRows = New-Object System.Collections.Generic.List[object]
            for ($i = 0; $i -lt $rankedPeers.Count; $i++) {
                $rp = $rankedPeers[$i]
                if ([int]$rp.SharedPatients -ne $prevVol) { $curRank = $i + 1; $prevVol = [int]$rp.SharedPatients }
                if ($rp.NPI -eq $primary) { $myRank = $curRank }
                if ([int]$rp.SharedPatients -gt 0) { $withVol++ }
                $outRows.Add([pscustomobject]@{
                    Rank = $curRank
                    You  = if ($rp.NPI -eq $primary) { '>> YOU' } else { '' }
                    NPI = $rp.NPI; Name = $rp.Name; Type = $rp.Type
                    City = $rp.City; State = $rp.State; Zip = $rp.Zip
                    DistanceMiles = $rp.DistanceMiles
                    ReferralSources = $rp.ReferralSources
                    SharedPatients = $rp.SharedPatients
                    SharePct = if ($regionTotal -gt 0) { [math]::Round(100.0 * $rp.SharedPatients / $regionTotal, 1) } else { 0 }
                    # A competitor that is really one clinic of a chain is a
                    # different threat from an independent of the same size.
                    Chain = if ($rp.Type -eq 'Organization') { Get-RmChainMark $rp.Name } else { '' }
                })
            }
            # The practice's own row is ALWAYS shown, appended after the top
            # 15 when it ranks below them (small practices would otherwise
            # never see themselves in their own report).
            $peersOut = @($outRows.ToArray() | Select-Object -First 15)
            if (-not @($peersOut | Where-Object { $_.You }).Count) {
                $peersOut = @($peersOut) + @($outRows.ToArray() | Where-Object { $_.You } | Select-Object -First 1)
            }
            $landscape = [pscustomobject]@{
                RadiusMiles         = $CompetitorRadiusMiles
                ZipCount            = $rzips.Count
                ProviderCount       = $rankedPeers.Count
                ProvidersWithVolume = $withVol
                RegionPatients      = $regionTotal
                Rank                = $myRank
                SharePct            = if ($regionTotal -gt 0) { [math]::Round(100.0 * $grandTotal / $regionTotal, 1) } else { 0 }
                Peers               = $peersOut
                Competitors         = @($outRows.ToArray() | Where-Object { -not $_.You } | Select-Object -First 5)
                SecondaryOnlyExcluded = $secondaryOnly
                ChainCount          = @($outRows.ToArray() | Where-Object { $_.Chain }).Count
            }

            # ---- Market-capture map layer -------------------------------
            # For every source ZIP: total therapy volume it sends into the
            # area vs the share this practice captures. Needs the LOCAL
            # NPPES index (thousands of source ZIPs in one scan); without it
            # the layer is skipped rather than firing thousands of API
            # lookups.
            if ((Test-Path -LiteralPath (Get-RmNppesIndexPath)) -and $areaBySource.Count -gt 0) {
                $srcWant = New-Object 'System.Collections.Generic.HashSet[string]'
                foreach ($k in $areaBySource.Keys) { [void]$srcWant.Add($k) }
                foreach ($srcRow2 in $sources) { [void]$srcWant.Add([string]$srcRow2.SourceNPI) }
                # Practice-therapist volume stays in the capture numerator:
                # competitors' area volumes are raw org inbound (including
                # THEIR staff), so both sides must follow one rule.
                foreach ($srcRow2 in $therRows) { [void]$srcWant.Add([string]$srcRow2.SourceNPI) }
                Write-Verbose "Market-capture layer: locating $($srcWant.Count) source provider(s) locally..."
                $srcZip = @{}
                # Sources already enriched (this practice's own, cached on
                # disk) are authoritative and free; the index fills in the
                # competitor-only sources. Using the index ALONE silently
                # dropped known sources whose ZIP was already in hand.
                $zipCache = Read-RmNppesCache
                foreach ($k in $srcWant) {
                    if ($zipCache.ContainsKey($k)) {
                        $cz = [string](Get-RmProp $zipCache[$k] 'Zip')
                        if ($cz -match '^\d{5}$') { $srcZip[$k] = $cz }
                    }
                }
                foreach ($line in @([RmEngine]::ScanRosterIndex((Get-RmNppesIndexPath), $srcWant, 0))) {
                    $ff = $line.Split('|')
                    if ($ff.Count -lt 10) { continue }
                    if ($srcZip.ContainsKey($ff[0])) { continue }
                    $pz = $ff[7]
                    if ($pz.Length -ge 5) { $srcZip[$ff[0]] = $pz.Substring(0, 5) }
                }
                $areaByZip = @{}
                foreach ($kv in $areaBySource.GetEnumerator()) {
                    $z = if ($srcZip.ContainsKey($kv.Key)) { $srcZip[$kv.Key] } else { '' }
                    if (-not $z -or -not $centroids.ContainsKey($z)) { continue }
                    if ($areaByZip.ContainsKey($z)) { $areaByZip[$z] += $kv.Value } else { $areaByZip[$z] = $kv.Value }
                }
                $mineByZip = @{}
                foreach ($gr in $geoRows) { $mineByZip[[string]$gr.Zip] = [int]$gr.SharedPatients }
                # CapturePct's numerator and denominator must share one
                # inclusion rule: competitor volume is located via the full
                # index sweep, so the practice's own sources BEYOND the
                # enrichment cap (absent from $geoRows) are located by that
                # same sweep and folded in - otherwise a 400+-source practice
                # under-reports its own capture in tail ZIPs.
                foreach ($srcRow3 in (@($sources) + @($therRows.ToArray()))) {
                    $sn = [string]$srcRow3.SourceNPI
                    if ($geoMappedNpis.Contains($sn)) { continue }
                    if (-not $srcZip.ContainsKey($sn)) { continue }
                    $z3 = $srcZip[$sn]
                    if (-not $centroids.ContainsKey($z3)) { continue }
                    if ($mineByZip.ContainsKey($z3)) { $mineByZip[$z3] += [int]$srcRow3.SharedPatients }
                    else { $mineByZip[$z3] = [int]$srcRow3.SharedPatients }
                }
                $allZips = New-Object 'System.Collections.Generic.HashSet[string]'
                foreach ($z in $areaByZip.Keys) { [void]$allZips.Add($z) }
                foreach ($z in $mineByZip.Keys) { [void]$allZips.Add($z) }
                $marketRows = foreach ($z in $allZips) {
                    $mineV = if ($mineByZip.ContainsKey($z)) { $mineByZip[$z] } else { 0 }
                    $compV = if ($areaByZip.ContainsKey($z)) { $areaByZip[$z] } else { 0 }
                    $areaV = $mineV + $compV
                    [pscustomobject]@{
                        Zip = $z
                        MyPatients = $mineV
                        AreaPatients = $areaV
                        CapturePct = if ($areaV -gt 0) { [math]::Round(100.0 * $mineV / $areaV, 1) } else { 0 }
                        Lat = $centroids[$z][0]; Lon = $centroids[$z][1]
                        DistanceMiles = if ($pracLoc) { Get-RmMilesBetween $pracLoc[0] $pracLoc[1] $centroids[$z][0] $centroids[$z][1] } else { '' }
                    }
                }
                $geoMarket = @($marketRows | Sort-Object -Property @{Expression = 'AreaPatients'; Descending = $true},
                                                                  @{Expression = 'Zip'; Descending = $false})
            }

            # ---- Outreach targets (missed sources) ----------------------
            # The biggest referrers feeding COMPARABLE providers in the
            # radius with NO measured flow into this practice - the sweep
            # already carried every one of these edges, so this is free.
            # Individual PT/OT/SLP "sources" of a competitor are that
            # competitor's own clinicians (the same co-billing pattern this
            # analysis folds out of its own numbers), so they are dropped:
            # nobody wins a referral from a rival's staff.
            if ($areaBySource.Count -gt 0) {
                $missCand = @($areaBySource.GetEnumerator() |
                    Where-Object { -not $mergedBySrc.ContainsKey($_.Key) } |
                    Sort-Object -Property @{Expression = { $_.Value }; Descending = $true},
                                          @{Expression = { $_.Key }; Descending = $false} |
                    Select-Object -First 40)
                $missNpis = @($missCand | ForEach-Object { $_.Key } | Where-Object { -not $detail.ContainsKey($_) })
                $missDetail = if ($missNpis.Count -gt 0) { Get-RmProviderDetail -Npi $missNpis } else { @{} }
                $missKeep = New-Object System.Collections.Generic.List[object]
                foreach ($mc in $missCand) {
                    if ($missKeep.Count -ge 20) { break }
                    $d = if ($detail.ContainsKey($mc.Key)) { $detail[$mc.Key] }
                         elseif ($missDetail.ContainsKey($mc.Key)) { $missDetail[$mc.Key] } else { $null }
                    $sp = if ($d) { ([string]$d.Specialty).ToUpperInvariant() } else { '' }
                    if ($sp -and $sp -notmatch 'CLINIC|CENTER' -and $sp -match $therapistRe) { continue }
                    $mz = if ($d -and $null -ne $d.PSObject.Properties['Zip'] -and ([string]$d.Zip) -match '^\d{5}$') { [string]$d.Zip } else { '' }
                    $missKeep.Add([pscustomobject]@{
                        SourceNPI = $mc.Key
                        SourceName = if ($d) { [string]$d.Name } else { '' }
                        SourceSpecialty = if ($d) { [string]$d.Specialty } else { '' }
                        City = if ($d) { [string]$d.City } else { '' }
                        State = if ($d) { [string]$d.State } else { '' }
                        PatientsToCompetitors = [int]$mc.Value
                        DistanceMiles = if ($mz -and $pracLoc -and $centroids.ContainsKey($mz)) {
                            Get-RmMilesBetween $pracLoc[0] $pracLoc[1] $centroids[$mz][0] $centroids[$mz][1]
                        } else { '' }
                    })
                }
                $missedRows = $missKeep.ToArray()
            }
        } catch {
            $landscapeNote = "COMPETITIVE LANDSCAPE unavailable for this run: $($_.Exception.Message)"
            Write-Warning $landscapeNote
        }
    }

    # Supplemental market context + real billed-services profile (both
    # keyless CMS open-data APIs, cached). Enrichment only: any failure
    # degrades to a note and never blocks the analysis.
    $market = $null; $svcProfile = $null; $suppNote = $null
    if (-not $SkipCompetitors) {   # same switch: skip in fast/offline runs
        try {
            if ($pracZip -match '^\d{5}$') { $market = Get-RmCountyMarket -Zip $pracZip -CrosswalkPath $CrosswalkPath }
        } catch { $suppNote = "MARKET CONTEXT unavailable: $($_.Exception.Message)"; Write-Warning $suppNote }
        try {
            $svcAll = Get-RmServiceProfile -Npi $npiList
            $svcAgg = [pscustomobject]@{
                TherapyServices = 0; MinDistinctPatients = 0; NpisWithClaims = 0; ClaimsSpecialty = '' }
            foreach ($v in $svcAll.Values) {
                $svcAgg.TherapyServices += [int]$v.TherapyServices
                # MAX, not sum: each per-NPI figure is a floor on that NPI's
                # distinct patients, but patients overlap across the NPIs of
                # one practice, so summed floors overstate the union. The max
                # is the largest bound that is still guaranteed true.
                if ([int]$v.MinDistinctPatients -gt $svcAgg.MinDistinctPatients) {
                    $svcAgg.MinDistinctPatients = [int]$v.MinDistinctPatients
                }
                if ($v.HasAnyClaims) { $svcAgg.NpisWithClaims++ }
                if (-not $svcAgg.ClaimsSpecialty -and $v.ClaimsSpecialty) { $svcAgg.ClaimsSpecialty = $v.ClaimsSpecialty }
            }
            $svcProfile = $svcAgg
        } catch { $suppNote = "BILLED-SERVICES PROFILE unavailable: $($_.Exception.Message)"; Write-Warning $suppNote }
    }

    $notes = @(Get-RmMethodologyNotes -Info $info) + @(
        ''
        "SOURCE ANALYSIS METHOD: every inbound pair of NPI $primary ($pracName) in $($info.Label). Individual PT/OT/SLP 'sources' are the practice's own clinicians billing under personal NPIs, so they are folded into the PRACTICE THERAPISTS figure below - the referral ranking, shares, concentration, geography and lag profile cover EXTERNAL sources only. PctOfVolume/CumulativePct are shares of the external referral volume."
        $(if ($therRows.Count) {
            $vst = @($therRows | Where-Object { $_.Staff -like 'VERIFIED*' }).Count
            "PRACTICE THERAPISTS: $($therRows.Count) individual PT/OT/SLP NPI(s) appear in the raw file as 'sources' of this practice, carrying $('{0:N0}' -f $therTotal) shared patients. That is the practice's own patient base arriving through its clinicians (PT->org co-billing), NOT external referrals, so it is summed into TotalPatients and kept out of the source ranking. $vst of $($therRows.Count) are on this practice's own Care Compare roster (VERIFIED staff); the rest are same-discipline clinicians - most commonly staff who left or are not yet on today's roster. An external therapist who truly refers here would also land in this bucket: check the roster labels before writing anyone off."
        })
        $(if (@($outboundTherRows).Count) {
            $ovst = @($outboundTherRows | Where-Object { $_.Staff -like 'VERIFIED*' }).Count
            "OUTBOUND OWN-CLINICIANS: $(@($outboundTherRows).Count) individual PT/OT/SLP NPI(s) also appear on the OUTBOUND side, carrying $('{0:N0}' -f $outTherPatients) patients - the same co-billing pattern in the other direction (continued care under the practice's own therapists), NOT a post-therapy hand-off, so they are folded out of the outbound destination list. $ovst of $(@($outboundTherRows).Count) are VERIFIED on this practice's Care Compare roster today."
        })
        $(if (@($atRisk).Count) {
            $arPat = 0; foreach ($ar in $atRisk) { $arPat += [int]$ar.SharedPatients }
            "AT-RISK REFERRERS: $(@($atRisk).Count) referrer-type source(s) carrying $('{0:N0}' -f $arPat) patients in $($info.Year) are flagged against TODAY's Medicare Order & Referring roster: " +
            ((@($atRisk | Sort-Object SharedPatients -Descending | Select-Object -First 8 | ForEach-Object { "$($_.SourceName) ($($_.SourceNPI); $($_.Status))" })) -join '; ') +
            "$(if (@($atRisk).Count -gt 8) { '; ...' }). A referrer who left the roster has retired, deactivated, or dis-enrolled - Medicare claims for their future referrals would deny. The check covers only provider types the roster lists (physicians, NPs, PAs, podiatry, optometry, dental, chiropractic, nurse specialists); organizations and therapists are never on it and are not checked."
        })
        $(if (@($missedRows).Count) {
            $msPat = 0; foreach ($ms in $missedRows) { $msPat += [int]$ms.PatientsToCompetitors }
            "OUTREACH TARGETS: the $(@($missedRows).Count) biggest referrers feeding comparable providers within $CompetitorRadiusMiles miles with NO measured flow into this practice carry $('{0:N0}' -f $msPat) patients to competitors in $($info.Year). Individual PT/OT/SLP 'sources' of a competitor are its own clinicians and are excluded. Pairs under 11 patients are invisible, so 'no measured flow' can also mean 'fewer than 11 patients came here' - treat the list as prospecting priorities, not proof of zero relationship."
        })
        $(if ($rosterRows.Count) { "PRACTICE ROSTER (Care Compare): $($rosterRows.Count) clinician(s) are listed under this practice in Medicare Care Compare today - matched by practice name, then expanded to everyone sharing the same group-enrollment id (org_pac_id). This is TODAY's roster: staff who left are absent even though their historical volume appears above, and cash-pay or non-Medicare clinicians never appear. If the location column shows an unexpected city, a same-named practice elsewhere matched too - read those rows with care." })
        $(if ($npiList.Count -gt 1) { "COMBINED ANALYSIS: inbound volume is merged across $($npiList.Count) NPIs ($($npiList -join ', ')). A source feeding several of them counts ONCE with summed volume; patient flows BETWEEN these NPIs are excluded as internal handoffs. Geography and the competitive radius are centered on the primary NPI ($primary)." })
        $(if ($grandTotal -gt 0 -and $grandTotal -lt 1000) { 'SMALL-PRACTICE NOTE: pairs under 11 distinct patients are excluded at the source, so a modest measured total usually UNDERSTATES the real referral base. Volume may also sit under the therapists'' individual NPIs — run a combined analysis (paste the org NPI plus the therapist NPIs together) for the full picture.' })
        'NET FLOW: SharedBack is the patients this practice shared ONWARD to that same source in the same year (claims sequence, both directions from one pass). A source with SharedPatients roughly equal to SharedBack and a long lag is co-occurring care (labs, pharmacies, hospitals); inbound far above SharedBack with a short lag is referral flow someone could win or lose.'
        'CONCENTRATION: HHI = sum of squared percentage shares (0-10,000); above ~2,500 is highly concentrated — losing one relationship materially moves the total. Top-1/5/10 dependence reads the same risk directly. Both are computed on MEASURED (11+ patient) pairs only: sub-floor referrers are invisible, which inflates the measured shares, so true concentration is LOWER whenever many small sources exist — treat a high reading on a short source list with caution.'
        'Distances are straight-line miles between ZIP-area centroids (US Census) using TODAY''s NPPES practice addresses — a source that moved is measured where it is now.'
        $(if ($isHop) { 'REFERRAL-LAG PROFILE: average days from source visit to this practice''s visit, volume-weighted, over EXTERNAL referral sources only. Short lags look like referrals; 90+ days usually means co-occurring care (labs, hospitals), not referral flow.' })
        $(if ($landscape) { "COMPETITIVE LANDSCAPE: peers are the NPPES-listed outpatient rehab providers (the same PT/OT/SLP taxonomy sweep the Referral map uses) whose registered - or, with the local NPPES index, secondary - practice location falls in the $($landscape.ZipCount) ZIP(s) within $($landscape.RadiusMiles) straight-line miles of ZIP $pracZip, ranked by inbound shared-patient volume on $($info.Label). DistanceMiles is to each provider's CLOSEST known location (registered or secondary site). Share of area volume = a provider's inbound volume over the SUM across all listed providers — share of measured referral VOLUME, not of patients." })
        $(if ($landscape) { 'A practice''s volume is often SPLIT between its organization NPI and its therapists'' individual NPIs, so a group can rank below its true combined volume. Benchmark the org NPI and its key therapists separately for the full picture.' })
        $(if ($landscape -and $landscape.SecondaryOnlyExcluded -gt 0) { "COMPARABILITY: $($landscape.SecondaryOnlyExcluded) provider(s) in the radius list a therapy taxonomy only in a SECONDARY slot — typically hospitals and multi-specialty organizations. They are excluded from the ranking because their inbound volume spans every service line, not therapy, and including them would overstate the market and understate this practice's share. A provider whose primary is a NON-SPECIFIC code - generic 'Clinic/Center', 'Multi-Specialty Clinic', or the legacy 'Specialist' - but which also carries a real therapy taxonomy is NOT excluded: those registrations are how chains and therapy companies fill in forms (277 of Athletico's 426 clinics, Apex Physical Therapy, EmpowerMe), and dropping them hid top-5 competitors in some markets." })
        $(if ($landscapeNote) { $landscapeNote })
        $(if ($market) { "MARKET CONTEXT: county Medicare enrollment from CMS's Medicare Monthly Enrollment dataset (calendar $($market.Year), latest full year). Original Medicare (FFS) beneficiaries are the population this file can see; Medicare Advantage members ($($market.MaPct)% of $($market.County)) are invisible to it." })
        $(if ($svcProfile) { 'BILLED-SERVICES PROFILE: actual Medicare Part B claims from CMS''s Physician & Other Practitioners dataset (latest annual release; therapy = HCPCS 97xxx/92xxx). CAUTION: this dataset suppresses provider-procedure lines under 11 beneficiaries, so small caseloads are invisible here too, and services bill under the RENDERING NPI — organizations that bill through their therapists'' individual NPIs legitimately show no claims here. Distinct-patient counts are a FLOOR (patients overlap across procedure codes).' })
        $(try {
            $aff = @(Get-RmAffiliatedNpi -Npi $primary | Where-Object { $npiList -notcontains $_.NPI })
            if ($aff.Count) {
                $names = @($aff | Select-Object -First 12 | ForEach-Object { "$($_.Name) ($($_.NPI))" }) -join ', '
                "AFFILIATED CLINICIANS (Care Compare): $($aff.Count) other therapy clinician(s) share this practice's group and are NOT in this analysis: $names$(if ($aff.Count -gt 12) { ', ...' }). Care they co-billed with this practice is already counted under PRACTICE THERAPISTS, but referrals sent DIRECTLY to their personal NPIs are not - paste their NPIs together with this one for the combined picture."
            } } catch { $null })
        $(if ($suppNote) { $suppNote })
        $(if ($capNote) { $capNote })
    ) | Where-Object { $null -ne $_ -and $_ -ne $false }

    [pscustomobject]@{
        Npi          = $primary
        NpiList      = @($npiList)
        NpiCount     = $npiList.Count
        Practice     = [pscustomobject]@{
            Name = $pracName; Zip = $pracZip
            City = if ($loc.Count) { [string](Get-RmProp $loc[0] 'city') } else { '' }
            State = if ($loc.Count) { [string](Get-RmProp $loc[0] 'state') } else { '' }
            Lat = if ($pracLoc) { $pracLoc[0] } else { $null }
            Lon = if ($pracLoc) { $pracLoc[1] } else { $null }
        }
        Year         = $info.Year
        Label        = $info.Label
        IsHop        = $isHop
        TotalPatients = $grandTotal          # full measured patient base (external + practice therapists)
        ReferralPatients = $total            # external referral volume only
        TherapistPatients = $therTotal       # billed by the practice's own PT/OT/SLP NPIs
        TherapistRows = @($therRows.ToArray())
        TherapistStaffVerified = @($therRows | Where-Object { $_.Staff -like 'VERIFIED*' }).Count
        PracticeRoster = @($rosterRows.ToArray())   # today's Care Compare roster (name + group-id matched)
        OutboundTherapistRows = @($outboundTherRows)  # own clinicians folded OUT of Outbound
        OutboundTherapistPatients = $outTherPatients
        SourceCount  = $sources.Count
        Top1Pct      = [double]$top1
        Top5Pct      = [math]::Round($top5, 1)
        Top10Pct     = [math]::Round($top10, 1)
        HHI          = [int][math]::Round($hhi, 0)
        Concentration = $concLabel
        Sources      = $sources
        SpecialtyMix = @($mix)
        DistanceBands = @($distBands)
        WaitBands    = @($waitBands)
        Geo          = @($geoRows)          # per-ZIP roll-up for the heat map
        GeoMarket    = @($geoMarket)        # per-ZIP area volume + capture rate
        GeoUnmappedPatients = $geoUnmapped  # volume with no locatable source ZIP
        DistantNote  = $distantNote         # $null when nothing is far away
        Market       = $market              # county Medicare market (CMS enrollment); $null offline
        ServiceProfile = $svcProfile        # real billed therapy claims (CMS P&S); $null offline
        Outbound     = @($outboundRows)   # free: the same pass carried both directions
        OutboundRawPatients = $(  $orp = 0; foreach ($ov in $outBySrc.Values) { $orp += [int]$ov.N }; $orp )
        OutboundRawDestinations = $outBySrc.Count
        MissedSources = @($missedRows)    # area referrers with no flow into this practice
        AtRiskSources = @($atRisk)        # referrer-type sources gone from the O&R roster
        Competitive  = $landscape     # $null when skipped or the sweep failed
        Trend        = $null          # filled by Add-RmSourceTrend
        Notes        = @($notes)
    }
}

function Get-RmSourceTrend {
    <#
    .SYNOPSIS
      Year-over-year referral PERFORMANCE for one practice (or several NPIs
      combined) across every imported CareSet year: volume, distinct sources,
      concentration, and — the part a single year cannot show — which
      referrers were kept, gained, and lost each year, plus the biggest
      movers between the first and last year.
    .NOTES
      Hop Teaming years only: they share one methodology, so they are
      comparable. The CMS 2015 file is excluded on purpose (~8-month window,
      different scale) — mixing it in would fake a trend. Each year is a full
      streaming scan, so expect minutes per year.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateCount(1, 50)][ValidatePattern('^\d{10}$')][string[]]$Npi,
        [switch]$SkipEnrichment,
        # A caller that already holds one year's per-source totals (the
        # analysis that just scanned it) passes them here to skip that
        # year's full 8 GB pass. Totals MUST be built the same way this
        # function builds them: internal flows excluded, summed per source.
        [int]$ReuseYear,
        [hashtable]$ReuseBySrc,
        # Raw outbound totals for the reused year (the analysis carries
        # them); -1 = unknown, that year's outbound columns stay blank.
        [int]$ReuseOutboundPatients = -1,
        [int]$ReuseOutboundDestinations = -1
    )
    $years = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' } | Sort-Object Year)
    if ($years.Count -lt 2) {
        throw ("A year-over-year analysis needs at least TWO imported CareSet years (found $($years.Count)). " +
               "Import more years with 'Import CareSet file' / Import-RmDataset.")
    }
    $memberSet = New-Object 'System.Collections.Generic.HashSet[string]'
    $npiList = @($Npi | Where-Object { $memberSet.Add($_) })

    # Per-year: merge across member NPIs exactly like the single-year
    # analysis (internal handoffs out, one row per external source).
    # A caller that already scanned one year (Add-RmSourceTrend riding on a
    # fresh analysis) hands that year's totals in, saving a full 8 GB pass.
    $perYear = @{}
    $perYearOut = @{}   # year -> @{ Patients; Destinations } ($null = unknown)
    foreach ($y in $years) {
        if ($ReuseBySrc -and $y.Year -eq $ReuseYear) {
            Write-Verbose "Reusing the already-scanned $($y.Label) totals..."
            $perYear[$y.Year] = $ReuseBySrc
            $perYearOut[$y.Year] = if ($ReuseOutboundPatients -ge 0) {
                @{ Patients = $ReuseOutboundPatients; Destinations = $ReuseOutboundDestinations }
            } else { $null }
            continue
        }
        Write-Verbose "Scanning $($y.Label) for $($npiList.Count) NPI(s)..."
        # ScanEither carries BOTH directions in the same single pass the
        # inbound-only scan used to make, so the outbound trend is free.
        $bySrc = @{}
        $outPat = 0
        $outDest = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($e in @([RmEngine]::ScanEither($y.Path, $memberSet, [RmEngine]::FormatHopTeaming))) {
            $srcIsMember = $memberSet.Contains($e.SourceNpi)
            $tgtIsMember = $memberSet.Contains($e.TargetNpi)
            if ($tgtIsMember -and -not $srcIsMember) {
                if (-not $bySrc.ContainsKey($e.SourceNpi)) { $bySrc[$e.SourceNpi] = 0 }
                $bySrc[$e.SourceNpi] += $e.BeneCount
            } elseif ($srcIsMember -and -not $tgtIsMember) {
                $outPat += $e.BeneCount
                [void]$outDest.Add($e.TargetNpi)
            }
        }
        $perYear[$y.Year] = $bySrc
        $perYearOut[$y.Year] = @{ Patients = $outPat; Destinations = $outDest.Count }
    }

    # Name the sources that matter: each year's top 25 PLUS the biggest
    # first-to-last movers. Without the movers a source that grew from
    # nothing into a major referrer would show as a bare NPI in the very
    # table meant to name it.
    $firstY0 = $years[0].Year; $lastY0 = $years[$years.Count - 1].Year
    $wanted = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($y in $years) {
        foreach ($kv in @($perYear[$y.Year].GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 25)) {
            [void]$wanted.Add($kv.Key)
        }
    }
    $deltas = @{}
    foreach ($k in @($perYear[$firstY0].Keys) + @($perYear[$lastY0].Keys)) {
        if ($deltas.ContainsKey($k)) { continue }
        $v0 = if ($perYear[$firstY0].ContainsKey($k)) { $perYear[$firstY0][$k] } else { 0 }
        $v1 = if ($perYear[$lastY0].ContainsKey($k)) { $perYear[$lastY0][$k] } else { 0 }
        $deltas[$k] = $v1 - $v0
    }
    foreach ($kv in @($deltas.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 15)) { [void]$wanted.Add($kv.Key) }
    foreach ($kv in @($deltas.GetEnumerator() | Sort-Object Value | Select-Object -First 15)) { [void]$wanted.Add($kv.Key) }
    $detail = @{}
    if (-not $SkipEnrichment -and $wanted.Count -gt 0) {
        $detail = Get-RmProviderDetail -Npi @($wanted)
    }

    # One row per year, with retention measured against the PRIOR year.
    # Retention only means "annual churn" when the prior IMPORTED year is the
    # prior CALENDAR year - a user holding 2019 and 2022 must not read "kept
    # 40%" as one year's churn when it spans three. Across a gap the columns
    # go blank (like the first year) and a note names the gap.
    $rows = New-Object System.Collections.Generic.List[object]
    $prevKeys = $null
    $prevYear = $null
    $gapPairs = New-Object System.Collections.Generic.List[string]
    foreach ($y in $years) {
        $bySrc = $perYear[$y.Year]
        $total = 0; foreach ($v in $bySrc.Values) { $total += $v }
        $ranked = @($bySrc.GetEnumerator() | Sort-Object Value -Descending)
        $hhi = 0.0
        foreach ($kv in $ranked) { if ($total -gt 0) { $s = 100.0 * $kv.Value / $total; $hhi += $s * $s } }
        $top1 = if ($ranked.Count -and $total -gt 0) { [math]::Round(100.0 * $ranked[0].Value / $total, 1) } else { 0 }
        $t5 = 0; foreach ($kv in @($ranked | Select-Object -First 5)) { $t5 += $kv.Value }
        $keys = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($k in $bySrc.Keys) { [void]$keys.Add($k) }
        $new = 0; $lost = 0; $kept = 0
        $consecutive = ($null -ne $prevKeys -and ([int]$y.Year - [int]$prevYear) -eq 1)
        if ($null -ne $prevKeys -and -not $consecutive) {
            $gapPairs.Add("$prevYear->$($y.Year)")
        }
        if ($consecutive) {
            foreach ($k in $keys) { if ($prevKeys.Contains($k)) { $kept++ } else { $new++ } }
            foreach ($k in $prevKeys) { if (-not $keys.Contains($k)) { $lost++ } }
        }
        $topName = if ($ranked.Count) {
            $d = if ($detail.ContainsKey($ranked[0].Key)) { $detail[$ranked[0].Key] } else { $null }
            if ($d -and $d.Name) { $d.Name } else { $ranked[0].Key }
        } else { '' }
        $rows.Add([pscustomobject]@{
            Year            = $y.Year
            SharedPatients  = $total
            SourceCount     = $ranked.Count
            Top1Pct         = $top1
            Top5Pct         = if ($total -gt 0) { [math]::Round(100.0 * $t5 / $total, 1) } else { 0 }
            HHI             = [int][math]::Round($hhi, 0)
            # First year has no prior year to compare against — and a year
            # after an import GAP has no prior CALENDAR year: blank, never
            # 0 — "0 new" would read as a measured result rather than "n/a".
            NewSources      = if ($consecutive) { $new } else { '' }
            RetainedSources = if ($consecutive) { $kept } else { '' }
            LostSources     = if ($consecutive) { $lost } else { '' }
            RetentionPct    = if ($consecutive -and $prevKeys.Count -gt 0) {
                                  [math]::Round(100.0 * $kept / $prevKeys.Count, 1) } else { '' }
            TopSource       = $topName
            # Outbound rides in the same pass; blank (never 0) for a reused
            # year whose caller could not supply raw outbound totals.
            OutboundPatients     = if ($perYearOut[$y.Year]) { [int]$perYearOut[$y.Year].Patients } else { '' }
            OutboundDestinations = if ($perYearOut[$y.Year]) { [int]$perYearOut[$y.Year].Destinations } else { '' }
        })
        $prevKeys = $keys
        $prevYear = $y.Year
    }

    # Biggest movers, first year vs last year.
    $firstY = $years[0].Year; $lastY = $years[$years.Count - 1].Year
    $first = $perYear[$firstY]; $last = $perYear[$lastY]
    $allSrc = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($k in $first.Keys) { [void]$allSrc.Add($k) }
    foreach ($k in $last.Keys) { [void]$allSrc.Add($k) }
    $movers = foreach ($k in $allSrc) {
        $a0 = if ($first.ContainsKey($k)) { $first[$k] } else { 0 }
        $b0 = if ($last.ContainsKey($k)) { $last[$k] } else { 0 }
        $d = if ($detail.ContainsKey($k)) { $detail[$k] } else { $null }
        [pscustomobject]@{
            SourceNPI       = $k
            SourceName      = if ($d) { $d.Name } else { '' }
            SourceSpecialty = if ($d) { $d.Specialty } else { '' }
            FirstYear       = $a0
            LastYear        = $b0
            Change          = $b0 - $a0
            Status          = if ($a0 -eq 0) { 'New' } elseif ($b0 -eq 0) { 'Lost' }
                              elseif ($b0 -gt $a0) { 'Grew' } elseif ($b0 -lt $a0) { 'Shrank' } else { 'Steady' }
        }
    }
    $movers = @($movers | Sort-Object -Property @{Expression = 'Change'; Descending = $true},
                                                @{Expression = 'SourceNPI'; Descending = $false})

    $firstRow = $rows[0]; $lastRow = $rows[$rows.Count - 1]
    $volChangePct = if ($firstRow.SharedPatients -gt 0) {
        [math]::Round(100.0 * ($lastRow.SharedPatients - $firstRow.SharedPatients) / $firstRow.SharedPatients, 1)
    } else { '' }

    # Leading years with nothing measured are the norm for a newer practice.
    # Say so, and measure growth from the first year that HAS volume too —
    # otherwise a 2019 practice looks like it did nothing until 2019.
    $leadingZero = New-Object System.Collections.Generic.List[object]
    foreach ($r in $rows) { if ($r.SharedPatients -le 0) { $leadingZero.Add($r.Year) } else { break } }
    $withVol = @($rows | Where-Object { $_.SharedPatients -gt 0 })
    $activeFrom = if ($withVol.Count) { $withVol[0].Year } else { $null }
    $activeChangePct = if ($withVol.Count -ge 2 -and $withVol[0].SharedPatients -gt 0) {
        [math]::Round(100.0 * ($withVol[$withVol.Count - 1].SharedPatients - $withVol[0].SharedPatients) / $withVol[0].SharedPatients, 1)
    } else { '' }

    $notes = @(
        "YEAR-OVER-YEAR METHOD: the same inbound scan repeated on each imported CareSet year ($(@($years | ForEach-Object { $_.Year }) -join ', ')) — same file format and same STATED methodology, full calendar years. (CareSet does not version its hop construction in the file itself, so a between-release methodology change cannot be detected here.)"
        'Retention is measured against the PRIOR CALENDAR year: Retained = sources present in both years, New = present this year only, Lost = present last year only. RetentionPct = Retained as a share of LAST year''s source count.'
        $(if ($gapPairs.Count) { "IMPORT GAP: the imported years are not consecutive ($($gapPairs -join ', ')). Retention/New/Lost are blank after a gap - churn measured across several years is not comparable to annual churn. Import the in-between years to fill them in." })
        'A source "lost" may simply have fallen under the 11-patient privacy floor rather than stopped referring — treat small movements as noise and read the direction of the whole base.'
        'Medicare FFS only: Medicare Advantage enrollment grew over these years, moving patients OUT of this data. A gentle decline can reflect that shift rather than lost referrals; compare against the area trend before concluding.'
        'OUTBOUND COLUMNS: OutboundPatients/OutboundDestinations are the RAW onward flow (everyone this practice shared patients to, including its own therapists) measured in the same pass. A blank means that year''s scan was reused from an older analysis that did not carry outbound totals - re-run to fill it.'
        'The CMS 2015 FOIA file is intentionally excluded: a different (~8-month) window and methodology, not on the same scale.'
        $(if ($leadingZero.Count) { "NO MEASURED VOLUME IN $($leadingZero -join ', '): the practice may not have been enumerated or billing Medicare yet in those years, or every pair it had fell under the 11-patient floor. Growth is therefore also reported from $activeFrom, the first year with measured volume." })
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Npi          = $npiList[0]
        NpiList      = @($npiList)
        Years        = @($rows.ToArray())
        FirstYear    = $firstY
        LastYear     = $lastY
        VolumeChangePct = $volChangePct
        ActiveFromYear  = $activeFrom          # first year with measured volume
        ActiveChangePct = $activeChangePct     # growth measured from that year
        Movers       = $movers
        Gained       = @($movers | Where-Object { $_.Change -gt 0 } | Select-Object -First 10)
        Lost         = @($movers | Where-Object { $_.Change -lt 0 } |
                            Sort-Object Change | Select-Object -First 10)
        Notes        = @($notes)
    }
}

function Add-RmSourceTrend {
    <#
    .SYNOPSIS
      Attaches a Get-RmSourceTrend result to a Get-RmSourceAnalysis result so
      one report can carry both the deep single-year view and the
      year-over-year performance. Returns the analysis object.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Analysis,
        [switch]$SkipEnrichment
    )
    $npis = if ($Analysis.PSObject.Properties['NpiList']) { @($Analysis.NpiList) } else { @($Analysis.Npi) }
    # The analysis just scanned the active year and its Sources table holds
    # exactly what the trend recomputes for that year (per-source totals,
    # internal flows already excluded) - hand it over instead of paying for
    # the same 8 GB pass twice. Only valid for a CareSet-year analysis.
    $trendArgs = @{ Npi = $npis; SkipEnrichment = $SkipEnrichment }
    if ($Analysis.PSObject.Properties['IsHop'] -and $Analysis.IsHop -and @($Analysis.Sources).Count) {
        $bySrc = @{}
        foreach ($srow in @($Analysis.Sources)) { $bySrc[[string]$srow.SourceNPI] = [int]$srow.SharedPatients }
        # The trend measures RAW per-year totals (it has no specialty data
        # for past years), so the practice-therapist rows the analysis split
        # out must ride along or the reused year would dip below its
        # neighbours by exactly the staff volume.
        if ($Analysis.PSObject.Properties['TherapistRows']) {
            foreach ($srow in @($Analysis.TherapistRows)) { $bySrc[[string]$srow.SourceNPI] = [int]$srow.SharedPatients }
        }
        $trendArgs['ReuseYear'] = [int]$Analysis.Year
        $trendArgs['ReuseBySrc'] = $bySrc
        if ($Analysis.PSObject.Properties['OutboundRawPatients']) {
            $trendArgs['ReuseOutboundPatients'] = [int]$Analysis.OutboundRawPatients
            $trendArgs['ReuseOutboundDestinations'] = [int]$Analysis.OutboundRawDestinations
        }
    }
    $Analysis.Trend = Get-RmSourceTrend @trendArgs
    $Analysis
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
    # MAP RADIUS CAP (same rule as the report's embedded map): a dot more
    # than 250 straight-line miles out is the registered corporate/HQ
    # address of a centralized service, not a place patients travel from.
    # It stays in the table and the totals; it is just not drawn.
    $mapFarCut = 250.0
    $mapFarRows = @($g.Rows | Where-Object { $null -ne $_.Lat -and $_.DistanceMiles -is [double] -and $_.DistanceMiles -gt $mapFarCut })
    $points = @($g.Rows | Where-Object { $null -ne $_.Lat } |
        Where-Object { -not ($_.DistanceMiles -is [double] -and $_.DistanceMiles -gt $mapFarCut) } | ForEach-Object {
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
    $mapCapNote = if (@($mapFarRows).Count) {
        $mapFarPat = 0; foreach ($fr in $mapFarRows) { $mapFarPat += [int]$fr.SharedPatients }
        "MAP RADIUS: $(@($mapFarRows).Count) ZIP area(s) carrying $('{0:N0}' -f $mapFarPat) patients sit more than $([int]$mapFarCut) miles from the practice and are NOT drawn on the map - at that distance a dot is the source's registered corporate/HQ address (reference labs, chains, telehealth), not a place patients travel from. Those rows stay in the table below and in every total."
    } else { $null }
    $notesHtml = (@(@($g.Notes) + @($mapCapNote)) | Where-Object { $_ } | ForEach-Object {
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
  /* Corporate palette: deep navy ink, bold cobalt accent, amber highlight.
     Sharp edges (2px radii), strong contrast, responsive from phone to
     desktop - wide tables scroll inside their card instead of breaking
     the page. */
  :root { --ink:#0f1f33; --sub:#4e5d6e; --line:#c9d3de; --accent:#0f5cad;
          --accent-dark:#0a3f78; --amber:#b45309; --bg:#e9edf2; }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:"Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif; }
  .wrap { max-width:1120px; margin:0 auto; padding:0 22px 36px; }
  header { display:flex; flex-wrap:wrap; align-items:center; gap:10px 16px;
           background:linear-gradient(135deg, var(--accent-dark), var(--accent));
           margin:0 -22px 14px; padding:20px 26px 18px;
           border-bottom:4px solid var(--amber); }
  header h1 { margin:0; color:#fff; font-size:23px; font-weight:700; letter-spacing:-.2px; }
  .badge { background:#fff; color:var(--accent-dark); font-size:11.5px; font-weight:700;
           padding:4px 12px; border-radius:2px; white-space:nowrap; letter-spacing:.4px; }
  .sub { color:var(--sub); font-size:13.5px; margin:2px 0 16px; }
  .stats { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 18px; }
  .stat { background:#fff; border:1px solid var(--line); border-top:3px solid var(--accent);
          border-radius:2px; padding:12px 18px; min-width:150px; flex:1 1 150px;
          box-shadow:0 1px 3px rgba(10,30,55,.10); }
  .stat b { display:block; font-size:23px; font-weight:700; letter-spacing:-.4px; color:var(--accent-dark);
            font-variant-numeric:tabular-nums; }
  .stat span { font-size:10.5px; color:var(--sub); text-transform:uppercase; letter-spacing:.7px; font-weight:600; }
  .stat.warn { border-top-color:#c0390f; }
  .stat.warn b { color:#c0390f; }
  .card { background:#fff; border:1px solid var(--line); border-radius:2px;
          box-shadow:0 1px 3px rgba(10,30,55,.10); margin-bottom:18px; padding:0 0 8px;
          overflow-x:auto; }
  .card h2 { margin:0; padding:14px 18px 10px; font-size:13.5px; font-weight:700;
             text-transform:uppercase; letter-spacing:.8px; color:var(--accent-dark);
             border-bottom:2px solid var(--accent); }
  .card .body { padding:10px 18px 8px; }
  .duo { display:flex; flex-wrap:wrap; gap:18px; }
  .duo > div { flex:1 1 460px; min-width:0; }
  /* Chart SVGs only — scoped to .body so Leaflet's attribute-sized overlay
     pane is untouched (a global svg rule collapsed it to 0x0 and made every
     map circle invisible; found by probing the rendered geometry). */
  .body svg { width:100%; height:auto; display:block; max-width:100%; }
  .bar { fill:var(--accent); }
  .blbl { font-size:12.5px; fill:#22364d; font-weight:600; }
  .bval { font-size:11.5px; fill:#44566b; }
  .dbig { font-size:26px; font-weight:700; fill:#0f1f33; }
  .dsm { font-size:11px; fill:#44566b; }
  .grid { stroke:#dfe6ed; stroke-width:1; }
  .axlbl { font-size:10.5px; fill:#5f7186; }
  .curve { fill:none; stroke:var(--accent); stroke-width:3; }
  .curve2 { fill:none; stroke:var(--amber); stroke-width:2.5; }
  .dot2 { fill:var(--amber); }
  .lbl2 { font-size:10.5px; fill:#8a5410; font-weight:600; }
  .bin { font-size:11.5px; fill:#ffffff; font-weight:700; }
  .segKept { fill:#0f5cad; } .segNew { fill:#5a92c9; } .segLost { fill:#c3cdd8; }
  .sub2 { margin:2px 0 0; padding:8px 18px 0; font-size:12.5px; font-weight:700; color:#22364d; }
  td.up { color:#116b3f; font-weight:700; }
  td.down { color:#c0390f; font-weight:700; }
  .findings { font-size:13.5px; line-height:1.7; margin:2px 0 6px; padding-left:22px; }
  .findings li { margin-bottom:6px; }
  .findings li::marker { color:var(--accent); font-weight:700; }
  .empty { font-size:13.5px; line-height:1.6; margin:2px 0 10px; color:#22364d; }
  table { border-collapse:collapse; width:100%; font-size:12.6px; }
  th, td { border-top:1px solid var(--line); padding:7px 12px; text-align:left; }
  th { background:var(--accent-dark); color:#fff; font-weight:700; font-size:10.5px;
       text-transform:uppercase; letter-spacing:.6px; border-top:none; white-space:nowrap; }
  tr:nth-child(even) td { background:#f2f6fa; }
  tr.you td { background:#dcebf8; font-weight:700; border-top:2px solid var(--accent); border-bottom:2px solid var(--accent); }
  .youtag { background:var(--amber); color:#fff; font-size:9.5px; font-weight:700;
            padding:2px 7px; border-radius:2px; vertical-align:1px; letter-spacing:.6px; }
  .proftbl th { text-align:left; white-space:nowrap; width:220px; background:#f2f6fa; color:#22364d;
                font-size:11px; vertical-align:top; padding:8px 12px; }
  .proftbl td { font-size:12.5px; padding:8px 12px; }
  .chain { color:var(--amber); font-weight:700; cursor:help; }
  .barmuted { fill:#96abc0; }
  #rm-map { height:52vh; min-height:380px; }
  .offline { padding:9px 14px; background:#fff3cd; color:#6b4e0e; font-size:12.5px;
             border-bottom:1px solid #e7d59a; display:none; }
  .prac-pin { width:22px; height:22px; border-radius:50%; background:#c62828;
              border:3px solid #fff; box-shadow:0 1px 6px rgba(0,0,0,.45); }
  .legend { background:#fff; padding:9px 12px; border-radius:2px;
            box-shadow:0 1px 5px rgba(0,0,0,.25); font-size:12px; line-height:19px; }
  .legend i { width:12px; height:12px; display:inline-block; border-radius:50%;
              margin-right:6px; vertical-align:-2px; }
  .maptools { display:flex; flex-wrap:wrap; align-items:center; gap:8px;
              padding:9px 16px; border-bottom:1px solid var(--line); background:#f2f6fa; }
  .mtlabel { font-size:11px; color:var(--sub); text-transform:uppercase; letter-spacing:.6px; font-weight:700; }
  .mtbtn { font:inherit; font-size:12.5px; padding:5px 12px; border-radius:2px; cursor:pointer;
           border:1px solid var(--line); background:#fff; color:#22364d; font-weight:600; }
  td.mono { font-variant-numeric:tabular-nums; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .mtbtn.active { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:700; }
  .mtchk { font-size:12.5px; color:#22364d; display:inline-flex; align-items:center; gap:5px; }
  .note { color:var(--sub); font-size:12px; }
  .tablenote { padding:8px 18px 10px; color:var(--sub); font-size:12px; }
  details { margin:0; } summary { cursor:pointer; padding:14px 18px; font-size:13.5px; font-weight:700;
            text-transform:uppercase; letter-spacing:.8px; color:var(--accent-dark); }
  .notes { font-size:12px; color:#435364; line-height:1.6; margin:0; padding:0 22px 14px 36px; }
  .notes li { margin-bottom:5px; }
  footer { color:var(--sub); font-size:11.5px; margin-top:6px; }
  /* Tablet */
  @media (max-width: 900px) {
    .wrap { padding:0 14px 28px; }
    header { margin:0 -14px 12px; padding:16px 18px 14px; }
    .duo > div { flex:1 1 100%; }
    .proftbl th { width:150px; white-space:normal; }
  }
  /* Phone */
  @media (max-width: 620px) {
    header h1 { font-size:19px; }
    .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .stat { min-width:0; padding:10px 12px; }
    .stat b { font-size:19px; }
    table { font-size:11.8px; }
    th, td { padding:6px 8px; }
    #rm-map { height:60vh; min-height:300px; }
    .card h2 { font-size:12.5px; }
  }
  @media print { body { background:#fff; } .card { box-shadow:none; overflow:visible; }
                 header { background:var(--accent-dark) !important; -webkit-print-color-adjust:exact; } }
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

function Export-RmSourceReportHtml {
    <#
    .SYNOPSIS
      Renders a Get-RmSourceAnalysis result as a self-contained, print-ready
      HTML report: KPI cards, top-source bar chart, specialty donut,
      concentration (Pareto) curve, distance and referral-lag profiles, the
      ranked source table, auto-written findings, full methodology, and an
      embedded referral-geography heat map (bundled Leaflet, inlined).
      Everything works offline except the map's street background tiles;
      the page says so plainly when they cannot load.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Analysis,
        [Parameter(Mandatory)][string]$Path,
        # One-stop extras. Each section renders only when its data is given,
        # so every existing caller keeps producing the same report.
        [object]$Provider,          # identity: Name/Specialty/City/State
        [string]$EligibilityLine,   # the Order & Referring status sentence
        [object[]]$Groups,          # practice-group memberships
        [object[]]$Outbound         # onward destinations (who they send to)
    )
    $a = $Analysis
    function _h([string]$t) { [System.Net.WebUtility]::HtmlEncode($t) }
    # With no measured inbound volume every source-side chart would be an
    # empty box and every derived metric meaningless; the report explains the
    # situation instead of rendering blanks that look broken.
    $hasVolume = ($a.TotalPatients -gt 0) -and (@($a.Sources).Count -gt 0)
    # Old analysis objects (pre practice-therapist split) lack these fields.
    $refPat = if ($a.PSObject.Properties['ReferralPatients']) { [int]$a.ReferralPatients } else { [int]$a.TotalPatients }
    $therPat = if ($a.PSObject.Properties['TherapistPatients']) { [int]$a.TherapistPatients } else { 0 }
    $therList = if ($a.PSObject.Properties['TherapistRows']) { @($a.TherapistRows) } else { @() }
    $rosterList = if ($a.PSObject.Properties['PracticeRoster']) { @($a.PracticeRoster) } else { @() }
    $outTherList = if ($a.PSObject.Properties['OutboundTherapistRows']) { @($a.OutboundTherapistRows) } else { @() }
    # Roster rows cross-referenced with measured volume: inbound patients
    # from the practice-therapist fold, outbound from the own-clinician fold.
    $rosterVol = @{}
    foreach ($t in $therList) { $rosterVol[[string]$t.SourceNPI] = @{ In = [int]$t.SharedPatients; Out = 0 } }
    foreach ($t in $outTherList) {
        $k = [string]$t.NPI
        if (-not $rosterVol.ContainsKey($k)) { $rosterVol[$k] = @{ In = 0; Out = 0 } }
        $rosterVol[$k].Out += [int]$t.SharedPatients
    }

    # ---- Chart 1: top-15 sources horizontal bars -------------------------
    $top = @($a.Sources | Select-Object -First 15)
    $maxV = 1; foreach ($t in $top) { if ($t.SharedPatients -gt $maxV) { $maxV = $t.SharedPatients } }
    $barH = 24; $gap = 8; $w = 980; $labelW = 330; $chartW = $w - $labelW - 90
    $h = ($top.Count * ($barH + $gap)) + 10
    $bars = New-Object System.Text.StringBuilder
    $y = 4
    foreach ($t in $top) {
        $bw = [int][math]::Max(2, $chartW * $t.SharedPatients / $maxV)
        $nm = if ($t.SourceName) { $t.SourceName } else { $t.SourceNPI }
        # 322px label column at 12.5px all-caps ≈ 40 chars before the text
        # would overflow the viewBox and be hard-clipped on the left.
        if ($nm.Length -gt 40) { $nm = $nm.Substring(0, 39) + '…' }
        [void]$bars.Append(('<text x="{0}" y="{1}" text-anchor="end" class="blbl">{2}</text>' -f ($labelW - 8), ($y + 16), (_h $nm)))
        [void]$bars.Append(('<rect x="{0}" y="{1}" width="{2}" height="{3}" rx="3" class="bar"/>' -f $labelW, $y, $bw, $barH))
        [void]$bars.Append(('<text x="{0}" y="{1}" class="bval">{2}  ({3}%)</text>' -f ($labelW + $bw + 6), ($y + 16), ('{0:N0}' -f $t.SharedPatients), $t.PctOfVolume))
        $y += $barH + $gap
    }
    $barSvg = ('<svg viewBox="0 0 {0} {1}" role="img" aria-label="Top referral sources">{2}</svg>' -f $w, $h, $bars.ToString())

    # ---- Chart 2: specialty donut (top 6 + other) ------------------------
    $palette = @('#2c5f8a', '#4f7ca6', '#7fa3c2', '#aec6da', '#c98f3d', '#7d8a96', '#c4cdd5')
    $mixTop = @($a.SpecialtyMix | Select-Object -First 6)
    # "Other" from raw VOLUMES, rounded once (100 minus a sum of rounded
    # slice percentages drifts; the audit flagged the pattern).
    $mixTotVol = 0; foreach ($m in @($a.SpecialtyMix)) { $mixTotVol += [int]$m.SharedPatients }
    $mixTopVol = 0; foreach ($m in $mixTop) { $mixTopVol += [int]$m.SharedPatients }
    $otherPct = if ($mixTop.Count -and $mixTotVol -gt 0) {
        [math]::Max(0, [math]::Round(100.0 * ($mixTotVol - $mixTopVol) / $mixTotVol, 1))
    } else { 0 }
    $segs = @($mixTop | ForEach-Object { [pscustomobject]@{ Label = $_.Specialty; Pct = [double]$_.PctOfVolume } })
    if ($otherPct -gt 0.05) { $segs = @($segs) + @([pscustomobject]@{ Label = 'Other'; Pct = $otherPct }) }
    $r = 70; $circ = 2 * [math]::PI * $r
    $donut = New-Object System.Text.StringBuilder
    $off = 0.0; $i = 0
    foreach ($sg in $segs) {
        $len = $circ * $sg.Pct / 100.0
        $circleFmt = '<circle cx="110" cy="110" r="{0}" fill="none" stroke="{1}" stroke-width="34" ' +
            'stroke-dasharray="{2:0.##} {3:0.##}" stroke-dashoffset="{4:0.##}" transform="rotate(-90 110 110)"/>'
        [void]$donut.Append(($circleFmt -f $r, $palette[$i % $palette.Count], $len, ($circ - $len), (-1 * $off)))
        $off += $len; $i++
    }
    $legend = New-Object System.Text.StringBuilder
    $ly = 30; $i = 0
    foreach ($sg in $segs) {
        $ll = $sg.Label; if ($ll.Length -gt 34) { $ll = $ll.Substring(0, 33) + '…' }
        [void]$legend.Append(('<rect x="240" y="{0}" width="12" height="12" rx="2" fill="{1}"/>' -f ($ly - 10), $palette[$i % $palette.Count]))
        [void]$legend.Append(('<text x="258" y="{0}" class="blbl">{1} — {2}%</text>' -f $ly, (_h $ll), $sg.Pct))
        $ly += 22; $i++
    }
    $donutFmt = '<svg viewBox="0 0 620 220" role="img" aria-label="Specialty mix">{0}{1}' +
        '<text x="110" y="105" text-anchor="middle" class="dbig">{2}</text>' +
        '<text x="110" y="126" text-anchor="middle" class="dsm">specialties</text></svg>'
    $donutSvg = $donutFmt -f $donut.ToString(), $legend.ToString(), @($a.SpecialtyMix).Count

    # ---- Chart 3: concentration (Pareto) curve ---------------------------
    $pw = 460; $ph = 200; $padL = 46; $padB = 28
    $n = [math]::Min(50, @($a.Sources).Count)
    $pts = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $n; $i++) {
        $x = $padL + (($pw - $padL - 10) * ($i + 1) / [math]::Max(1, $n))
        $yv = ($ph - $padB) - (($ph - $padB - 12) * ([double]@($a.Sources)[$i].CumulativePct / 100.0))
        [void]$pts.Append(('{0:0.#},{1:0.#} ' -f $x, $yv))
    }
    $gridLines = New-Object System.Text.StringBuilder
    foreach ($pct in 25, 50, 75, 100) {
        $gy = ($ph - $padB) - (($ph - $padB - 12) * $pct / 100.0)
        [void]$gridLines.Append(('<line x1="{0}" y1="{1:0.#}" x2="{2}" y2="{1:0.#}" class="grid"/>' -f $padL, $gy, ($pw - 10)))
        [void]$gridLines.Append(('<text x="{0}" y="{1:0.#}" text-anchor="end" class="axlbl">{2}%</text>' -f ($padL - 6), ($gy + 4), $pct))
    }
    $paretoFmt = '<svg viewBox="0 0 {0} {1}" role="img" aria-label="Cumulative concentration">{2}' +
        '<polyline points="{3}" class="curve"/>' +
        '<text x="{4}" y="{5}" class="axlbl">sources, ranked by volume (top {6})</text></svg>'
    $paretoSvg = $paretoFmt -f $pw, $ph, $gridLines.ToString(), $pts.ToString().Trim(), $padL, ($ph - 8), $n

    # ---- Chart 4/5: distance + wait band columns -------------------------
    function _bandSvg($bands, $aria) {
        $bw2 = 460; $bh = 190; $bpad = 40
        $bandList = @($bands)
        $maxP = 1.0; foreach ($b in $bandList) { if ($b.Pct -gt $maxP) { $maxP = [double]$b.Pct } }
        $colW = [int](($bw2 - $bpad - 10) / [math]::Max(1, $bandList.Count)) - 14
        $sb = New-Object System.Text.StringBuilder
        $x = $bpad + 6
        foreach ($b in $bandList) {
            $colH = [int][math]::Max(2, ($bh - 58) * ([double]$b.Pct / $maxP))
            $cy = ($bh - 34) - $colH
            [void]$sb.Append(('<rect x="{0}" y="{1}" width="{2}" height="{3}" rx="3" class="bar"/>' -f $x, $cy, $colW, $colH))
            [void]$sb.Append(('<text x="{0}" y="{1}" text-anchor="middle" class="bval">{2}%</text>' -f ($x + [int]($colW / 2)), ($cy - 5), $b.Pct))
            [void]$sb.Append(('<text x="{0}" y="{1}" text-anchor="middle" class="axlbl">{2}</text>' -f ($x + [int]($colW / 2)), ($bh - 16), (_h ([string]$b.Band))))
            $x += $colW + 14
        }
        ('<svg viewBox="0 0 {0} {1}" role="img" aria-label="{2}">{3}</svg>' -f $bw2, $bh, $aria, $sb.ToString())
    }
    $distSvg = _bandSvg $a.DistanceBands 'Referral volume by distance'
    $waitSvg = if ($a.IsHop -and @($a.WaitBands).Count) { _bandSvg $a.WaitBands 'Referral volume by lag' } else { '' }

    # ---- Chart 6 + table: competitive landscape (when the sweep ran) -----
    $comp = if ($a.PSObject.Properties['Competitive']) { $a.Competitive } else { $null }
    $compHtml = ''
    if ($comp -and @($comp.Peers).Count) {
        # Chart and table only providers with measured volume (plus this
        # practice, always). In a small market most listed providers have
        # none, and a column of zero-length bars is noise, not information —
        # the count of those providers is stated in the note instead.
        $cpShown = @($comp.Peers | Where-Object { [int]$_.SharedPatients -gt 0 -or $_.You })
        $cpTop = @($cpShown | Select-Object -First 10)
        if (-not @($cpTop | Where-Object { $_.You }).Count) {
            $cpTop = @($cpTop) + @($cpShown | Where-Object { $_.You } | Select-Object -First 1)
        }
        $cpMax = 1; foreach ($p in $cpTop) { if ($p.SharedPatients -gt $cpMax) { $cpMax = $p.SharedPatients } }
        $cpBars = New-Object System.Text.StringBuilder
        $cy = 4
        foreach ($p in $cpTop) {
            $cbw = [int][math]::Max(2, $chartW * $p.SharedPatients / $cpMax)
            $cnm = [string]$p.Name
            if ($p.You) { $cnm += ' (you)' }
            if ($cnm.Length -gt 40) { $cnm = $cnm.Substring(0, 39) + '…' }
            $cls = if ($p.You) { 'bar' } else { 'barmuted' }
            [void]$cpBars.Append(('<text x="{0}" y="{1}" text-anchor="end" class="blbl">{2}</text>' -f ($labelW - 8), ($cy + 16), (_h $cnm)))
            [void]$cpBars.Append(('<rect x="{0}" y="{1}" width="{2}" height="{3}" rx="3" class="{4}"/>' -f $labelW, $cy, $cbw, $barH, $cls))
            [void]$cpBars.Append(('<text x="{0}" y="{1}" class="bval">{2}  ({3}%)</text>' -f ($labelW + $cbw + 6), ($cy + 16), ('{0:N0}' -f $p.SharedPatients), $p.SharePct))
            $cy += $barH + $gap
        }
        $cpSvg = ('<svg viewBox="0 0 {0} {1}" role="img" aria-label="Competitive landscape">{2}</svg>' -f $w, ($cpTop.Count * ($barH + $gap) + 10), $cpBars.ToString())
        $cpRowFmt = '<tr{0}><td class="num">{1}</td><td>{2}</td><td>{3}</td><td>{4}</td>' +
            '<td class="num">{5}</td><td class="num">{6}</td><td class="num">{7}</td><td class="num">{8}%</td></tr>'
        $cpRows = (@($cpShown) | ForEach-Object {
            $tag = if ($_.You) { ' class="you"' } else { '' }
            # A chain competitor gets the asterisk right on its name: the row
            # is ONE of its clinics, so the company is bigger than it looks.
            $chainMark = if ($_.PSObject.Properties['Chain'] -and $_.Chain) { ' <span class="chain" title="One clinic of a multi-site company">*</span>' } else { '' }
            $nmCell = (_h ([string]$_.Name)) + $chainMark + $(if ($_.You) { ' <span class="youtag">YOU</span>' } else { '' })
            $miles = if ($_.DistanceMiles -is [double]) { '{0:N1}' -f $_.DistanceMiles } else { '' }
            $cpRowFmt -f $tag, $_.Rank, $nmCell,
                (_h ([string]$_.Type)),
                (_h (("{0}, {1} {2}" -f $_.City, $_.State, $_.Zip).Trim(', ').Trim())),
                $miles, ('{0:N0}' -f $_.ReferralSources), ('{0:N0}' -f $_.SharedPatients), $_.SharePct
        }) -join "`n"
        $cpHidden = [math]::Max(0, $comp.ProviderCount - @($cpShown).Count)
        $cpNote = ("$('{0:N0}' -f $comp.ProviderCount) outpatient rehab provider$(if ($comp.ProviderCount -ne 1) { 's' }) " +
            "$(if ($comp.ProviderCount -eq 1) { 'is' } else { 'are' }) listed within $($comp.RadiusMiles) miles; " +
            "$('{0:N0}' -f $comp.ProvidersWithVolume) $(if ($comp.ProvidersWithVolume -eq 1) { 'has' } else { 'have' }) measured referral volume in $($a.Year).") +
            $(if ($cpHidden -gt 0) { " The other $('{0:N0}' -f $cpHidden) $(if ($cpHidden -eq 1) { 'is' } else { 'are' }) not shown here — every pair they had (if any) fell under the 11-patient privacy floor." }) +
            $(if ($comp.PSObject.Properties['ChainCount'] -and $comp.ChainCount -gt 0) {
                " An asterisk (*) marks a competitor that is one clinic of a MULTI-SITE COMPANY — its organization name is registered by more than $($script:RmChainNpiThreshold) organization NPIs, so the row is a slice of a larger operator, not an independent practice. $($comp.ChainCount) of the ranked providers carry it." })
        $compHtml = @"
<div class="card"><h2>Competitive landscape &mdash; inbound referral volume within $($comp.RadiusMiles) miles</h2>
<div class="body">$cpSvg</div>
<table>
  <tr><th class="num">#</th><th>Practice</th><th>Type</th><th>Location</th>
      <th class="num">Miles</th><th class="num">Sources</th><th class="num">Patients</th><th class="num">Area share</th></tr>
  $cpRows
</table>
<div class="tablenote">$cpNote The analyzed practice's own row is always shown, even when it ranks below the top 15. Equal volumes share a rank. A practice's volume can be split across its organization and individual therapist NPIs.</div>
</div>
"@
    }

    # ---- Outreach targets (missed sources) -------------------------------
    $missList = if ($a.PSObject.Properties['MissedSources']) { @($a.MissedSources) } else { @() }
    $outreachHtml = ''
    if (@($missList).Count) {
        $mRows = (@($missList) | ForEach-Object {
            '<tr><td class="mono">{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td class="num">{4}</td><td class="num">{5}</td></tr>' -f
                $_.SourceNPI, (_h ([string]$_.SourceName)), (_h ([string]$_.SourceSpecialty)),
                (_h (("{0}, {1}" -f $_.City, $_.State).Trim(', ').Trim())),
                $_.DistanceMiles, ('{0:N0}' -f [int]$_.PatientsToCompetitors)
        }) -join "`n"
        $mTot = 0; foreach ($ms in $missList) { $mTot += [int]$ms.PatientsToCompetitors }
        $mRad = if ($comp) { $comp.RadiusMiles } else { 10 }
        $outreachHtml = @"
<div class="card"><h2>Outreach targets &mdash; area referrers not feeding this practice</h2>
<div class="body"><p>The $(@($missList).Count) biggest referrers sending patients to comparable providers within $mRad miles with <b>no measured flow into this practice</b>: $('{0:N0}' -f $mTot) patients went from them to competitors in $($a.Year). Competitors' own PT/OT/SLP clinicians are excluded &mdash; nobody wins a referral from a rival's staff. Pairs under 11 patients are invisible, so "no measured flow" can also mean "fewer than 11 came here": treat this as a prospecting priority list, not proof of zero relationship.</p></div>
<table>
  <tr><th>NPI</th><th>Referrer</th><th>Specialty</th><th>Location</th><th class="num">Miles</th><th class="num">Patients to competitors</th></tr>
  $mRows
</table>
</div>
"@
    }

    # ---- Year-over-year performance (when a trend is attached) -----------
    $tr = if ($a.PSObject.Properties['Trend']) { $a.Trend } else { $null }
    # ---- One-stop sections: provider profile + outbound destinations -----
    $profileHtml = ''
    # @($null).Count is 1, so a bare @(...).Count check would render these
    # sections for every existing caller - guard on null explicitly.
    $groupsGiven = ($null -ne $Groups)
    $outboundGiven = ($null -ne $Outbound -and @($Outbound | Where-Object { $_ }).Count -gt 0)
    if ($Provider -or $EligibilityLine -or $groupsGiven) {
        $rows = New-Object System.Text.StringBuilder
        if ($Provider -and $Provider.Specialty) {
            [void]$rows.Append('<tr><th>Specialty</th><td>' + (_h ([string]$Provider.Specialty)) + '</td></tr>')
        }
        if ($EligibilityLine) {
            [void]$rows.Append('<tr><th>Medicare ordering &amp; referring</th><td>' + (_h $EligibilityLine) + '</td></tr>')
        }
        if ($groupsGiven -and @($Groups | Where-Object { $_ }).Count) {
            $gtxt = (@($Groups | Where-Object { $_ } | ForEach-Object { "$($_.GroupName) [$($_.State), roster $($_.RosterSize)]" }) -join '; ')
            [void]$rows.Append('<tr><th>Practice group(s)</th><td>' + (_h $gtxt) + '</td></tr>')
        } elseif ($groupsGiven) {
            [void]$rows.Append('<tr><th>Practice group(s)</th><td>None on record &mdash; solo, or not reassigning to a group.</td></tr>')
        }
        $profileHtml = ('<div class="card"><h2>Provider profile</h2><table class="proftbl">' + $rows.ToString() + '</table></div>')
    }
    $outHtml = ''
    if ($outboundGiven) {
        $oTop = @($Outbound | Where-Object { $_ } | Sort-Object SharedPatients -Descending | Select-Object -First 15)
        $oIsHop = [bool]($oTop[0].PSObject.Properties['AvgDayWait'])
        $oRows = (@($oTop) | ForEach-Object {
            $lastCell = if ($oIsHop) { '{0:N1}' -f [double]$_.AvgDayWait } else { '{0:N0}' -f [int]$_.SameDay }
            '<tr><td>' + (_h ([string]$_.Name)) + '</td><td>' + (_h ([string]$_.Specialty)) + '</td><td>' + $_.NPI +
            '</td><td class="num">' + ('{0:N0}' -f [int]$_.SharedPatients) + '</td><td class="num">' + $lastCell + '</td></tr>'
        }) -join "`n"
        $oTotal = 0; foreach ($o in @($Outbound | Where-Object { $_ })) { $oTotal += [int]$o.SharedPatients }
        $outHtml = @"
<div class="card"><h2>Where patients go next &mdash; outbound destinations</h2>
<table>
  <tr><th>Destination</th><th>Specialty</th><th>NPI</th><th class="num">Patients</th><th class="num">$(if ($oIsHop) { 'Avg day wait' } else { 'Same day' })</th></tr>
  $oRows
</table>
<p class="note">$('{0:N0}' -f @($Outbound | Where-Object { $_ }).Count) destination(s), $('{0:N0}' -f $oTotal) patients shared onward in $($a.Year) &mdash; the physicians, imaging centers, and facilities that own the post-therapy hand-offs. Top 15 shown; the direction is claims sequence, so co-occurring care appears alongside true referrals.$(if (@($outTherList).Count) { $foldPat = 0; foreach ($ot in $outTherList) { $foldPat += [int]$ot.SharedPatients }; " <b>$(@($outTherList).Count) of the practice's own PT/OT/SLP clinicians ($('{0:N0}' -f $foldPat) patients) were folded out of this list</b> &mdash; continued care under the practice's own therapists, not an outbound hand-off. They are listed in the roster section below." })</p>
</div>
"@
    }

    $trendHtml = ''
    if ($tr -and @($tr.Years).Count -ge 2) {
        $ty = @($tr.Years)
        # Chart A: volume columns per year with a source-count line over them.
        $tw = 620; $th = 250; $tpadL = 52; $tpadB = 42; $tpadT = 22
        $maxVol = 1; foreach ($r in $ty) { if ($r.SharedPatients -gt $maxVol) { $maxVol = $r.SharedPatients } }
        $maxSrc = 1; foreach ($r in $ty) { if ($r.SourceCount -gt $maxSrc) { $maxSrc = $r.SourceCount } }
        $slotW = ($tw - $tpadL - 16) / [math]::Max(1, $ty.Count)
        $colW = [int][math]::Min(58, $slotW * 0.6)
        $tsb = New-Object System.Text.StringBuilder
        $linePts = New-Object System.Text.StringBuilder
        $plotH = $th - $tpadB - $tpadT
        for ($i = 0; $i -lt $ty.Count; $i++) {
            $r = $ty[$i]
            $cx = $tpadL + ($slotW * $i) + ($slotW / 2.0)
            $colH = [int][math]::Max(1, $plotH * $r.SharedPatients / $maxVol)
            $cy = $th - $tpadB - $colH
            [void]$tsb.Append(('<rect x="{0:0.#}" y="{1}" width="{2}" height="{3}" rx="3" class="bar"/>' -f ($cx - $colW / 2.0), $cy, $colW, $colH))
            # Value INSIDE the column top when there is room: the source-count
            # line runs above the columns and would otherwise strike through
            # a label sitting just outside them.
            if ($colH -ge 26) {
                [void]$tsb.Append(('<text x="{0:0.#}" y="{1}" text-anchor="middle" class="bin">{2}</text>' -f $cx, ($cy + 17), ('{0:N0}' -f $r.SharedPatients)))
            } else {
                [void]$tsb.Append(('<text x="{0:0.#}" y="{1}" text-anchor="middle" class="bval">{2}</text>' -f $cx, ($cy - 6), ('{0:N0}' -f $r.SharedPatients)))
            }
            [void]$tsb.Append(('<text x="{0:0.#}" y="{1}" text-anchor="middle" class="axlbl">{2}</text>' -f $cx, ($th - $tpadB + 16), $r.Year))
            $ly2 = $th - $tpadB - ($plotH * $r.SourceCount / $maxSrc)
            [void]$linePts.Append(('{0:0.#},{1:0.#} ' -f $cx, $ly2))
        }
        # the source-count line + its dots, drawn over the columns
        [void]$tsb.Append(('<polyline points="{0}" class="curve2"/>' -f $linePts.ToString().Trim()))
        for ($i = 0; $i -lt $ty.Count; $i++) {
            $r = $ty[$i]
            $cx = $tpadL + ($slotW * $i) + ($slotW / 2.0)
            $ly2 = $th - $tpadB - ($plotH * $r.SourceCount / $maxSrc)
            [void]$tsb.Append(('<circle cx="{0:0.#}" cy="{1:0.#}" r="3.5" class="dot2"/>' -f $cx, $ly2))
            [void]$tsb.Append(('<text x="{0:0.#}" y="{1:0.#}" text-anchor="middle" class="lbl2">{2}</text>' -f $cx, ($ly2 - 9), $r.SourceCount))
        }
        [void]$tsb.Append(('<text x="6" y="{0}" class="axlbl">patients</text>' -f ($tpadT - 8)))
        [void]$tsb.Append(('<text x="{0}" y="{1}" text-anchor="end" class="lbl2">— distinct sources</text>' -f ($tw - 6), ($tpadT - 8)))
        $trVolSvg = ('<svg viewBox="0 0 {0} {1}" role="img" aria-label="Referral volume by year">{2}</svg>' -f $tw, $th, $tsb.ToString())

        # Chart B: source retention per year (retained / new / lost).
        $rsb = New-Object System.Text.StringBuilder
        $rYears = @($ty | Select-Object -Skip 1)      # year 1 has no prior year
        if ($rYears.Count) {
            $rw = 620; $rh = 210; $rpadL = 52; $rpadB = 40; $rpadT = 20
            $maxStack = 1
            foreach ($r in $rYears) { $s = $r.RetainedSources + $r.NewSources + $r.LostSources; if ($s -gt $maxStack) { $maxStack = $s } }
            $rslot = ($rw - $rpadL - 16) / [math]::Max(1, $rYears.Count)
            $rcolW = [int][math]::Min(46, $rslot * 0.5)
            $rplotH = $rh - $rpadB - $rpadT
            for ($i = 0; $i -lt $rYears.Count; $i++) {
                $r = $rYears[$i]
                $cx = $rpadL + ($rslot * $i) + ($rslot / 2.0)
                $yTop = $rh - $rpadB
                foreach ($seg in @(
                    @{ V = $r.RetainedSources; C = 'segKept' }
                    @{ V = $r.NewSources; C = 'segNew' }
                    @{ V = $r.LostSources; C = 'segLost' })) {
                    if ($seg.V -le 0) { continue }
                    $segH = [int][math]::Max(1, $rplotH * $seg.V / $maxStack)
                    $yTop -= $segH
                    [void]$rsb.Append(('<rect x="{0:0.#}" y="{1}" width="{2}" height="{3}" class="{4}"/>' -f ($cx - $rcolW / 2.0), $yTop, $rcolW, $segH, $seg.C))
                }
                [void]$rsb.Append(('<text x="{0:0.#}" y="{1}" text-anchor="middle" class="axlbl">{2}</text>' -f $cx, ($rh - $rpadB + 16), $r.Year))
                if ($r.RetentionPct -ne '') {
                    [void]$rsb.Append(('<text x="{0:0.#}" y="{1}" text-anchor="middle" class="bval">{2}% kept</text>' -f $cx, ($yTop - 6), $r.RetentionPct))
                }
            }
            foreach ($lg in @(
                @{ X = $rpadL; T = 'retained'; C = 'segKept' }
                @{ X = $rpadL + 96; T = 'new'; C = 'segNew' }
                @{ X = $rpadL + 168; T = 'lost'; C = 'segLost' })) {
                [void]$rsb.Append(('<rect x="{0}" y="{1}" width="10" height="10" rx="2" class="{2}"/>' -f $lg.X, ($rpadT - 16), $lg.C))
                [void]$rsb.Append(('<text x="{0}" y="{1}" class="axlbl">{2}</text>' -f ($lg.X + 15), ($rpadT - 7), $lg.T))
            }
            $trRetSvg = ('<svg viewBox="0 0 {0} {1}" role="img" aria-label="Source retention by year">{2}</svg>' -f $rw, $rh, $rsb.ToString())
        } else { $trRetSvg = '' }

        # Older trend objects predate the outbound columns - render them
        # only when at least one year carries a value.
        $hasTrOut = $false
        foreach ($tyr in @($ty)) {
            if ($null -ne $tyr.PSObject.Properties['OutboundPatients'] -and $tyr.OutboundPatients -ne '') { $hasTrOut = $true; break }
        }
        $yrRowFmt = '<tr><td class="num">{0}</td><td class="num">{1}</td><td class="num">{2}</td>' +
            '<td class="num">{3}</td><td class="num">{4}</td><td class="num">{5}</td><td class="num">{6}</td>{8}<td>{7}</td></tr>'
        $yrRows = (@($ty) | ForEach-Object {
            $outCells = if ($hasTrOut) {
                $op = if ($null -ne $_.PSObject.Properties['OutboundPatients']) { $_.OutboundPatients } else { '' }
                $od = if ($null -ne $_.PSObject.Properties['OutboundDestinations']) { $_.OutboundDestinations } else { '' }
                '<td class="num">' + $(if ($op -eq '') { '&ndash;' } else { '{0:N0}' -f [int]$op }) +
                '</td><td class="num">' + $(if ($od -eq '') { '&ndash;' } else { '{0:N0}' -f [int]$od }) + '</td>'
            } else { '' }
            $yrRowFmt -f $_.Year, ('{0:N0}' -f $_.SharedPatients), ('{0:N0}' -f $_.SourceCount),
                ('{0:N0}' -f $_.HHI), $_.Top5Pct,
                $(if ($_.RetentionPct -eq '') { '&ndash;' } else { "$($_.RetentionPct)%" }),
                $(if ($_.NewSources -eq '') { '&ndash;' } else { '{0:N0}' -f $_.NewSources }),
                (_h ([string]$_.TopSource)), $outCells
        }) -join "`n"

        $movFmt = '<tr><td>{0}</td><td>{1}</td><td class="num">{2}</td><td class="num">{3}</td><td class="num {4}">{5}</td></tr>'
        function _movRows($rowsIn) {
            (@($rowsIn) | ForEach-Object {
                $nm = if ($_.SourceName) { $_.SourceName } else { "NPI $($_.SourceNPI)" }
                $cls = if ($_.Change -gt 0) { 'up' } else { 'down' }
                $sign = if ($_.Change -gt 0) { '+' } else { '' }
                $movFmt -f (_h ([string]$nm)), (_h ([string]$_.SourceSpecialty)),
                    ('{0:N0}' -f $_.FirstYear), ('{0:N0}' -f $_.LastYear), $cls, ($sign + ('{0:N0}' -f $_.Change))
            }) -join "`n"
        }
        $gainRows = _movRows $tr.Gained
        $lossRows = _movRows $tr.Lost
        $movHead = '<tr><th>Source</th><th>Specialty</th><th class="num">' + $tr.FirstYear +
            '</th><th class="num">' + $tr.LastYear + '</th><th class="num">Change</th></tr>'

        $trendHtml = @"
<div class="card"><h2>Year-over-year performance &mdash; $($tr.FirstYear) to $($tr.LastYear)</h2>
<div class="body">$trVolSvg</div>
$(if ($trRetSvg) { '<h3 class="sub2">Referral-source retention (vs the prior year)</h3><div class="body">' + $trRetSvg + '</div>' })
<table>
  <tr><th class="num">Year</th><th class="num">Patients</th><th class="num">Sources</th><th class="num">HHI</th>
      <th class="num">Top-5 %</th><th class="num">Kept</th><th class="num">New</th>$(if ($hasTrOut) { '<th class="num">Outbound</th><th class="num">Destinations</th>' })<th>Largest source</th></tr>
  $yrRows
</table>
<div class="tablenote">Kept = share of the previous year's sources still present. Sources under 11 shared patients are excluded every year, so a source can appear or vanish by crossing that floor rather than by winning or losing the relationship.$(if ($hasTrOut) { ' Outbound/Destinations = RAW onward flow (including the practice''s own therapists), measured in the same pass; a dash is a year whose scan predates these columns.' })</div>
</div>
<div class="duo">
  <div class="card"><h2>Biggest gains, $($tr.FirstYear) &rarr; $($tr.LastYear)</h2>
    <table>$movHead
$gainRows
    </table></div>
  <div class="card"><h2>Biggest declines, $($tr.FirstYear) &rarr; $($tr.LastYear)</h2>
    <table>$movHead
$lossRows
    </table></div>
</div>
"@
    }

    # ---- Referral geography heat map (bundled Leaflet, inlined) ----------
    # Same embedding rules as the standalone map: the library is inserted by
    # LITERAL replacement after the here-string expands (147 KB of minified
    # JS must never pass through string interpolation), and a typeof-L guard
    # explains an offline background instead of showing a broken white box.
    # Unlike the standalone map there is NO CDN fallback — this report's
    # contract is fully-self-contained, so a missing bundle skips the map.
    $geo = if ($a.PSObject.Properties['Geo']) { @($a.Geo) } else { @() }
    $geoCardHtml = ''
    $lfCss = ''; $lfJs = ''
    if ($hasVolume -and $geo.Count -gt 0) {
        $lfDir = Join-Path $PSScriptRoot 'leaflet'
        $lfJsPath = Join-Path $lfDir 'leaflet.min.js'
        $lfCssPath = Join-Path $lfDir 'leaflet.css'
        if ((Test-Path -LiteralPath $lfJsPath) -and (Test-Path -LiteralPath $lfCssPath)) {
            $lfCss = '<style>' + [System.IO.File]::ReadAllText($lfCssPath) + '</style>'
            $lfJs = '<script>' + [System.IO.File]::ReadAllText($lfJsPath) + '</script>'
            # MAP RADIUS CAP: a dot more than 250 straight-line miles out is
            # almost never a place patients travel from - it is the REGISTERED
            # corporate/HQ address of a centralized service (reference labs,
            # chains, telehealth). Those rows keep their place in every table
            # and total; they are just not DRAWN, so one New York dot cannot
            # zoom a Colorado map out to the whole country.
            $geoFarCut = 250.0
            $geoFarRows = @($geo | Where-Object { $_.DistanceMiles -is [double] -and $_.DistanceMiles -gt $geoFarCut })
            $geoDrawn = @($geo | Where-Object { -not ($_.DistanceMiles -is [double] -and $_.DistanceMiles -gt $geoFarCut) })
            $geoFarPat = 0; foreach ($gr in $geoFarRows) { $geoFarPat += [int]$gr.SharedPatients }
            $geoPts = @($geoDrawn | ForEach-Object {
                $prov = @($_.TopProviders | ForEach-Object {
                    [ordered]@{ n = [string]$_.Name; s = [string]$_.Specialty; p = [int]$_.Patients } })
                [ordered]@{
                    z = $_.Zip; lat = [double]$_.Lat; lon = [double]$_.Lon
                    p = [int]$_.SharedPatients; s = [int]$_.Sources
                    city = [string]$_.City; st = [string]$_.State
                    d = [string]$_.DistanceMiles; top = [string]$_.TopSource
                    pct = [double]$_.PctOfVolume
                    prov = @($prov)
                }
            })
            $geoJson = ConvertTo-Json -InputObject @($geoPts) -Compress -Depth 5
            $geoPrac = ConvertTo-Json -InputObject ([ordered]@{
                name = [string]$a.Practice.Name; zip = [string]$a.Practice.Zip
                city = [string]$a.Practice.City; st = [string]$a.Practice.State
                lat = $a.Practice.Lat; lon = $a.Practice.Lon
            }) -Compress
            # Market-capture layer + competitor pins (present only when the
            # local NPPES index made them computable).
            $mkt = if ($a.PSObject.Properties['GeoMarket']) { @($a.GeoMarket) } else { @() }
            $mktJson = ConvertTo-Json -Compress -Depth 4 -InputObject @(@($mkt) | ForEach-Object {
                [ordered]@{ z = $_.Zip; lat = [double]$_.Lat; lon = [double]$_.Lon
                            mine = [int]$_.MyPatients; area = [int]$_.AreaPatients
                            cap = [double]$_.CapturePct
                            d = [string]$_.DistanceMiles }
            })
            $compPts = @()
            if ($comp) {
                $compPts = @(@($comp.Peers | Where-Object { -not $_.You -and [int]$_.SharedPatients -gt 0 }) | ForEach-Object {
                    [ordered]@{ n = [string]$_.Name; z = [string]$_.Zip; p = [int]$_.SharedPatients
                                city = [string]$_.City; st = [string]$_.State }
                })
            }
            $compJson = ConvertTo-Json -Compress -Depth 4 -InputObject @($compPts)
            $geoMapped = 0; foreach ($gr in $geo) { $geoMapped += [int]$gr.SharedPatients }
            $geoUn = if ($a.PSObject.Properties['GeoUnmappedPatients']) { [int]$a.GeoUnmappedPatients } else { 0 }
            $zipRowsHtml = (@($geo | Select-Object -First 15) | ForEach-Object {
                $z = [string]$_.Zip
                $mrow = @($mkt | Where-Object { $_.Zip -eq $z })
                $capCell = if ($mrow.Count) { ('{0}%' -f $mrow[0].CapturePct) } else { '&ndash;' }
                $areaCell = if ($mrow.Count) { '{0:N0}' -f $mrow[0].AreaPatients } else { '&ndash;' }
                '<tr><td class="mono">{0}</td><td>{1}</td><td>{2}</td><td class="num">{3}</td><td class="num">{4}</td><td class="num">{5}%</td><td class="num">{6}</td><td class="num">{7}</td><td>{8}</td></tr>' -f
                    (_h $z), (_h ([string]$_.City)), (_h ([string]$_.State)),
                    ('{0:N0}' -f $_.Sources), ('{0:N0}' -f $_.SharedPatients), $_.PctOfVolume,
                    $areaCell, $capCell, (_h ([string]$_.TopSource))
            }) -join "`n"
            $zipNote = "Top $([math]::Min(15, $geo.Count)) of $('{0:N0}' -f $geo.Count) source ZIP areas; " +
                "$('{0:N0}' -f $geoMapped) of $('{0:N0}' -f $refPat) external-source patients mappable" +
                $(if ($geoUn -gt 0) { " ($('{0:N0}' -f $geoUn) from sources without a locatable ZIP)" }) + '.' +
                $(if (@($geoFarRows).Count) { " MAP RADIUS: $(@($geoFarRows).Count) ZIP area(s) carrying $('{0:N0}' -f $geoFarPat) patients sit more than $([int]$geoFarCut) miles away and are NOT drawn - at that distance a dot is the source's registered corporate/HQ address (reference labs, chains, telehealth), not a place patients travel from. Those rows stay in this table and in every total." }) +
                $(if ($a.PSObject.Properties['DistantNote'] -and $a.DistantNote) { ' ' + $a.DistantNote })
            $capNoteHtml = if (@($mkt).Count) {
                'AREA / CAPTURE columns: total therapy volume that ZIP sends to ANY comparable provider within the radius, and this practice''s share of it. A big ZIP with a low capture rate is an outreach target.'
            } else {
                'Capture-rate columns need the local NPPES index (see INSTRUCTIONS: Import-RmNppesBulk) — without it the area-wide comparison is skipped rather than estimated.'
            }
            $geoCardHtml = @"
<div class="card">
  <h2>Referral geography &mdash; where the volume comes from</h2>
  <div id="rm-offline" class="offline">The background street map could not load (no internet connection?). The circles and the table below still work.</div>
  <div class="maptools">
    <span class="mtlabel">Show:</span>
    <button type="button" class="mtbtn active" data-layer="mine">My referral volume</button>
    $(if (@($mkt).Count) { '<button type="button" class="mtbtn" data-layer="capture">Market capture rate</button>' })
    $(if (@($compPts).Count) { '<label class="mtchk"><input type="checkbox" id="rm-comp"/> Competitor locations</label>' })
    <label class="mtchk"><input type="checkbox" id="rm-rings" checked/> Distance rings</label>
  </div>
  <div id="rm-map"></div>
  <table>
    <tr><th>ZIP</th><th>City</th><th>St</th><th class="num">Sources</th><th class="num">My patients</th><th class="num">% of vol</th><th class="num">Area vol</th><th class="num">Capture</th><th>Top source in ZIP</th></tr>
    $zipRowsHtml
  </table>
  <div class="tablenote">$zipNote $capNoteHtml Click any circle for the named providers in that ZIP. Drawing the street background needs an internet connection &mdash; everything else in this report works without one.</div>
</div>
<script>
(function(){
  var pts = $geoJson;
  var prac = $geoPrac;
  var mkt = $mktJson;
  var comps = $compJson;
  if (typeof L === 'undefined') {
    document.getElementById('rm-offline').style.display = 'block';
    document.getElementById('rm-map').style.height = '0';
    return;
  }
  var maxP = 1; pts.forEach(function(p){ if (p.p > maxP) maxP = p.p; });
  var maxA = 1; mkt.forEach(function(m){ if (m.area > maxA) maxA = m.area; });
  var center = (prac.lat !== null) ? [prac.lat, prac.lon]
             : (pts.length ? [pts[0].lat, pts[0].lon] : [39.5, -98.35]);
  var map = L.map('rm-map').setView(center, 10);
  var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 18, attribution: '&copy; OpenStreetMap contributors' });
  tiles.on('tileerror', function(){ document.getElementById('rm-offline').style.display='block'; });
  tiles.addTo(map);
  function volColor(v) {
    var t = Math.sqrt(v / maxP);
    return 'rgb(' + Math.round(43 + t*172) + ',' + Math.round(131 - t*106) + ',' + Math.round(186 - t*158) + ')';
  }
  // Capture rate: red = you capture little of a real market (opportunity),
  // green = you already own it. Deliberately NOT the volume ramp, so the
  // two layers can never be mistaken for each other.
  function capColor(pct) {
    if (pct >= 60) return '#1a7f4b';
    if (pct >= 30) return '#7cb342';
    if (pct >= 15) return '#f9a825';
    if (pct >= 5)  return '#ef6c00';
    return '#c62828';
  }
  function esc(x) { return String(x == null ? '' : x).replace(/[<>&]/g, function(ch){
    return ch === '<' ? '&lt;' : ch === '>' ? '&gt;' : '&amp;'; }); }

  var mineLayer = L.layerGroup(), capLayer = L.layerGroup(),
      compLayer = L.layerGroup(), ringLayer = L.layerGroup();

  pts.forEach(function(p) {
    var lines = (p.prov || []).map(function(x){
      return '&bull; ' + esc(x.n) + (x.s ? ' <i>(' + esc(x.s) + ')</i>' : '') + ' &mdash; ' + x.p.toLocaleString(); });
    L.circleMarker([p.lat, p.lon], {
      radius: 6 + 30 * Math.sqrt(p.p / maxP), color: '#26333e', weight: 1,
      fillColor: volColor(p.p), fillOpacity: 0.72
    }).addTo(mineLayer).bindPopup('<b>ZIP ' + p.z + '</b> &mdash; ' + esc(p.city) + ', ' + esc(p.st) +
      '<br/>' + p.p.toLocaleString() + ' patients (' + p.pct + '% of your volume) from ' + p.s + ' source(s)' +
      (p.d ? '<br/>' + p.d + ' miles from the practice' : '') +
      (lines.length ? '<br/><br/><b>Top sources here:</b><br/>' + lines.join('<br/>') : ''));
  });

  mkt.forEach(function(m) {
    var lost = m.area - m.mine;
    L.circleMarker([m.lat, m.lon], {
      radius: 6 + 30 * Math.sqrt(m.area / maxA), color: '#26333e', weight: 1,
      fillColor: capColor(m.cap), fillOpacity: 0.75
    }).addTo(capLayer).bindPopup('<b>ZIP ' + m.z + '</b>' +
      '<br/>Area therapy volume: ' + m.area.toLocaleString() +
      '<br/>Yours: ' + m.mine.toLocaleString() + ' (<b>' + m.cap + '%</b>)' +
      '<br/>To other providers: ' + lost.toLocaleString() +
      (m.d ? '<br/>' + m.d + ' miles from the practice' : ''));
  });

  comps.forEach(function(c) {
    var pt = null;
    for (var i = 0; i < mkt.length; i++) { if (mkt[i].z === c.z) { pt = mkt[i]; break; } }
    if (!pt) { for (var k = 0; k < pts.length; k++) { if (pts[k].z === c.z) { pt = pts[k]; break; } } }
    if (!pt) return;
    L.circleMarker([pt.lat, pt.lon], { radius: 5, color: '#4a148c', weight: 2,
      fillColor: '#ce93d8', fillOpacity: 0.9 })
      .addTo(compLayer).bindPopup('<b>' + esc(c.n) + '</b><br/>' + esc(c.city) + ', ' + esc(c.st) +
        '<br/>' + c.p.toLocaleString() + ' inbound patients (competitor)');
  });

  if (prac.lat !== null) {
    [5, 10, 25].forEach(function(mi) {
      L.circle([prac.lat, prac.lon], { radius: mi * 1609.34, fill: false,
        color: '#5b6b7a', weight: 1, dashArray: '4 5', opacity: 0.65 }).addTo(ringLayer);
    });
    ringLayer.addTo(map);
  }
  mineLayer.addTo(map);

  if (prac.lat !== null) {
    L.marker([prac.lat, prac.lon], { title: prac.name,
      icon: L.divIcon({ className: 'prac-pin', iconSize: [22, 22], iconAnchor: [11, 11] }) })
      .addTo(map).bindPopup('<b>' + esc(prac.name) + '</b><br/>' + esc(prac.city) + ', ' + esc(prac.st) + ' ' + prac.zip + '<br/>(the practice)');
  }

  var legend = L.control({position:'bottomright'});
  var mode = 'mine';
  function legendHtml() {
    if (mode === 'capture') {
      return '<b>Your share of ZIP volume</b><br/>' +
        '<i style="background:#c62828"></i>under 5% &mdash; open market<br/>' +
        '<i style="background:#ef6c00"></i>5-15%<br/>' +
        '<i style="background:#f9a825"></i>15-30%<br/>' +
        '<i style="background:#7cb342"></i>30-60%<br/>' +
        '<i style="background:#1a7f4b"></i>60%+ &mdash; you own it<br/>' +
        'Circle size = total area volume';
    }
    return '<b>Patients from ZIP</b><br/>' +
      '<i style="background:' + volColor(maxP) + '"></i>' + maxP.toLocaleString() + ' (max)<br/>' +
      '<i style="background:' + volColor(maxP/4) + '"></i>~' + Math.round(maxP/4).toLocaleString() + '<br/>' +
      '<i style="background:' + volColor(maxP/20) + '"></i>~' + Math.round(maxP/20).toLocaleString() + '<br/>' +
      'Red pin = the practice';
  }
  legend.onAdd = function() {
    var div = L.DomUtil.create('div', 'legend');
    div.id = 'rm-legend';
    div.innerHTML = legendHtml();
    return div;
  };
  legend.addTo(map);

  var btns = document.querySelectorAll('.mtbtn');
  for (var b = 0; b < btns.length; b++) {
    btns[b].addEventListener('click', function() {
      for (var q = 0; q < btns.length; q++) { btns[q].className = 'mtbtn'; }
      this.className = 'mtbtn active';
      mode = this.getAttribute('data-layer');
      if (mode === 'capture') { map.removeLayer(mineLayer); capLayer.addTo(map); }
      else { map.removeLayer(capLayer); mineLayer.addTo(map); }
      var el = document.getElementById('rm-legend');
      if (el) { el.innerHTML = legendHtml(); }
    });
  }
  var cchk = document.getElementById('rm-comp');
  if (cchk) { cchk.addEventListener('change', function() {
    if (this.checked) { compLayer.addTo(map); } else { map.removeLayer(compLayer); } }); }
  var rchk = document.getElementById('rm-rings');
  if (rchk) { rchk.addEventListener('change', function() {
    if (this.checked) { ringLayer.addTo(map); } else { map.removeLayer(ringLayer); } }); }

  var fitPts = pts.slice().sort(function(x, y){ return y.p - x.p; });
  var tot = 0; fitPts.forEach(function(p){ tot += p.p; });
  var fit = []; var acc = 0;
  for (var i = 0; i < fitPts.length; i++) {
    fit.push(L.latLng(fitPts[i].lat, fitPts[i].lon));
    acc += fitPts[i].p;
    if (acc >= tot * 0.9) break;
  }
  if (prac.lat !== null) { fit.push(L.latLng(prac.lat, prac.lon)); }
  if (fit.length > 1) { map.fitBounds(L.latLngBounds(fit).pad(0.18)); }
})();
</script>
"@
        }
    }

    # ---- Auto-written findings ------------------------------------------
    $s1 = if (@($a.Sources).Count) { @($a.Sources)[0] } else { $null }   # @()[0] throws under StrictMode
    # From band VOLUMES rounded once, not a sum of the bands' rounded
    # percentages (that pattern drifts; the audit flagged it).
    $nearVol = 0; foreach ($b in @($a.DistanceBands)) { if ($b.Band -in '0-5 mi', '5-10 mi') { $nearVol += [int]$b.SharedPatients } }
    $near = if ($refPat -gt 0) { [math]::Round(100.0 * $nearVol / $refPat, 1) } else { 0 }
    $findings = @(
        $(if ($therPat -gt 0) {
            "The practice's measured Medicare patient base was $('{0:N0}' -f $a.TotalPatients) shared patients in $($a.Year): $('{0:N0}' -f $refPat) from $('{0:N0}' -f $a.SourceCount) external referral source(s), plus $('{0:N0}' -f $therPat) billed by its own $(@($therList).Count) PT/OT/SLP clinician(s) (their volume is the practice's own caseload, not referrals)."
        } else {
            "The practice drew $('{0:N0}' -f $a.TotalPatients) shared Medicare patients from $('{0:N0}' -f $a.SourceCount) distinct sources in $($a.Year)."
        })
        $(if ($s1) { "The single largest source, $(if ($s1.SourceName) { $s1.SourceName } else { "NPI $($s1.SourceNPI)" }), accounts for $($s1.PctOfVolume)% of inbound volume; the top five account for $($a.Top5Pct)% and the top ten for $($a.Top10Pct)%." })
        $(if ($a.PSObject.Properties['AtRiskSources'] -and @($a.AtRiskSources).Count) {
            $arp = 0; foreach ($ar in @($a.AtRiskSources)) { $arp += [int]$ar.SharedPatients }
            $arTop = @($a.AtRiskSources | Sort-Object SharedPatients -Descending | Select-Object -First 3 | ForEach-Object { "$($_.SourceName)" })
            "AT RISK: $(@($a.AtRiskSources).Count) referral source(s) carrying $('{0:N0}' -f $arp) patients in $($a.Year) are no longer Medicare order-&-refer eligible today (retired, deactivated, or dis-enrolled) - starting with $($arTop -join ', '). Their future Medicare referrals would deny; the source table flags each one."
        })
        $(if ($hasVolume) { "Source concentration is $($a.Concentration) (HHI $('{0:N0}' -f $a.HHI) on a 0-10,000 scale)." })
        $(if ($hasVolume) {
            if ($near -gt 0) { "$near% of measured volume originates within 10 miles of the practice." }
            else { 'None of the measured volume originates within 10 miles — this practice draws from a wider region than its immediate area.' } })
        $(if ($hasVolume -and $a.IsHop -and @($a.WaitBands).Count) {
            $fastVol = 0; foreach ($b in @($a.WaitBands)) { if ($b.Band -in '0-7 days', '7-30 days') { $fastVol += [int]$b.SharedPatients } }
            $fast = [math]::Round(100.0 * $fastVol / [math]::Max(1, $refPat), 1)
            "$fast% of volume arrives within 30 days of the source visit (referral-like); the remainder reflects looser or co-occurring care patterns." })
        $(if ($a.PSObject.Properties['Market'] -and $a.Market) {
            $mk = $a.Market
            # Divide data-year volume by SAME-year FFS enrollment when that
            # year is on hand (FFS shrinks as MA grows, so the latest year's
            # denominator would flatter the rate); fall back to the latest
            # year, saying so. Old disk caches predate FfsByYear.
            $den = 0; $denYear = 0
            if ($mk.PSObject.Properties['FfsByYear'] -and $mk.FfsByYear) {
                $p = $mk.FfsByYear.PSObject.Properties[[string]$a.Year]
                if ($p -and ([string]$p.Value) -match '^\d+$' -and [int]$p.Value -gt 0) { $den = [int]$p.Value; $denYear = [int]$a.Year }
            }
            if (-not $den -and $mk.FfsBenes -gt 0) { $den = [int]$mk.FfsBenes; $denYear = [int]$mk.Year }
            $perK = if ($den -gt 0 -and $a.TotalPatients -gt 0) { [math]::Round(1000.0 * $a.TotalPatients / $den, 1) } else { $null }
            "The practice's county ($($mk.County), $($mk.State)) had $('{0:N0}' -f $mk.TotalBenes) Medicare beneficiaries in $($mk.Year); $($mk.MaPct)% were in Medicare Advantage and are invisible to this data." +
            $(if ($perK) { " Measured shared-patient volume equals $perK per 1,000 Original-Medicare beneficiaries countywide ($denYear enrollment$(if ($denYear -ne [int]$a.Year) { " — the $($a.Year) county figure was not on hand, so the rate mixes years" }); sum semantics, so a patient with several sources counts once per source)." }) })
        $(if ($a.PSObject.Properties['ServiceProfile'] -and $a.ServiceProfile) {
            $sp = $a.ServiceProfile
            if ($sp.TherapyServices -gt 0) {
                "CMS claims data shows $('{0:N0}' -f $sp.TherapyServices) billed Medicare therapy services under $(if ($sp.NpisWithClaims -eq 1) { 'this NPI' } else { "$($sp.NpisWithClaims) of these NPIs" }) in the latest annual release, reaching at least $('{0:N0}' -f $sp.MinDistinctPatients) distinct patients."
            } elseif ($sp.NpisWithClaims -eq 0) {
                'CMS claims data shows no directly-billed Part B therapy lines above its 11-beneficiary line floor under ' +
                $(if ($a.NpiCount -gt 1) { 'these NPIs' } else { 'this NPI' }) +
                ' — organizations typically bill under their therapists'' individual NPIs (add those NPIs for a combined view).'
            } })
        $(if ($comp -and $comp.Rank -and $a.TotalPatients -gt 0) {
            "Among $('{0:N0}' -f $comp.ProviderCount) outpatient rehab providers within $($comp.RadiusMiles) miles, the practice ranks #$($comp.Rank) by inbound Medicare referral volume, holding $($comp.SharePct)% of the area's measured volume." })
        $(if ($comp -and $a.TotalPatients -eq 0) {
            "The file shows no measured inbound volume for this practice — every pair (if any) fell under the 11-patient privacy floor. $('{0:N0}' -f $comp.ProvidersWithVolume) of $('{0:N0}' -f $comp.ProviderCount) area providers do show measured volume." })
        $(if ($a.TotalPatients -gt 0 -and $a.TotalPatients -lt 1000) {
            'Note: pairs under 11 patients are excluded at the source, so a modest measured total usually understates the real referral base — and volume may sit under individual therapist NPIs (a combined multi-NPI analysis captures both).' })
        $(if ($tr -and @($tr.Years).Count -ge 2 -and $tr.VolumeChangePct -ne '') {
            $t0 = @($tr.Years)[0]; $t1 = @($tr.Years)[@($tr.Years).Count - 1]
            $dir = if ($tr.VolumeChangePct -gt 0) { 'grew' } elseif ($tr.VolumeChangePct -lt 0) { 'declined' } else { 'held flat' }
            $mag = [math]::Abs($tr.VolumeChangePct)
            "Measured referral volume $dir $mag% from $($tr.FirstYear) to $($tr.LastYear) ($('{0:N0}' -f $t0.SharedPatients) to $('{0:N0}' -f $t1.SharedPatients) patients), while the distinct-source count went from $('{0:N0}' -f $t0.SourceCount) to $('{0:N0}' -f $t1.SourceCount)." })
        $(if ($tr -and $tr.PSObject.Properties['ActiveChangePct'] -and $tr.VolumeChangePct -eq '' -and $tr.ActiveChangePct -ne '') {
            $dir2 = if ($tr.ActiveChangePct -gt 0) { 'grew' } elseif ($tr.ActiveChangePct -lt 0) { 'declined' } else { 'held flat' }
            "No volume was measured before $($tr.ActiveFromYear); from that first active year to $($tr.LastYear), measured referral volume $dir2 $([math]::Abs($tr.ActiveChangePct))%." })
        $(if ($tr -and @($tr.Years).Count -ge 2) {
            $tl = @($tr.Years)[@($tr.Years).Count - 1]
            if ($tl.RetentionPct -ne '') {
                "In $($tl.Year) the practice kept $($tl.RetentionPct)% of the prior year's referral sources, added $('{0:N0}' -f $tl.NewSources) and lost $('{0:N0}' -f $tl.LostSources)." } })
        $(if ($comp -and @($comp.Competitors).Count) {
            $c1 = @($comp.Competitors)[0]
            if ([int]$c1.SharedPatients -gt 0) {
                "Its largest competitor is $($c1.Name) (#$($c1.Rank) in the area) with $('{0:N0}' -f $c1.SharedPatients) shared patients — $($c1.SharePct)% of area volume."
            } else {
                'No other provider in the area shows measured inbound volume (pairs under 11 patients are excluded from the data).'
            } })
    ) | Where-Object { $_ }
    $findingsHtml = (@($findings) | ForEach-Object { '<li>' + (_h ([string]$_)) + '</li>' }) -join "`n"

    # ---- Tables ----------------------------------------------------------
    # Older analysis objects predate the SharedBack / Eligibility columns.
    $hasBack = ($null -ne $s1 -and $null -ne $s1.PSObject.Properties['SharedBack'])
    $anyRisk = $false
    $srcRowsHtml = (@($a.Sources | Select-Object -First 25) | ForEach-Object {
        $extra = if ($a.IsHop) { '<td class="num">{0:N1}</td>' -f [double]$_.AvgDayWait } else { '' }
        $backCell = if ($hasBack) { '<td class="num">' + ('{0:N0}' -f [int]$_.SharedBack) + '</td>' } else { '' }
        $risk = ($null -ne $_.PSObject.Properties['Eligibility'] -and $_.Eligibility -and $_.Eligibility -ne 'eligible')
        if ($risk) { $anyRisk = $true }
        $nm = (_h ([string]$_.SourceName))
        if ($risk) { $nm = '<b>&#9888;</b> ' + $nm }
        $rowFmt = '<tr><td class="num">{0}</td><td class="mono">{1}</td><td>{2}</td><td>{3}</td><td>{4}</td>' +
         '<td class="num">{5}</td>{10}<td class="num">{6}%</td><td class="num">{7}%</td><td class="num">{8}</td>{9}</tr>'
        $rowFmt -f
            $_.Rank, (_h ([string]$_.SourceNPI)), $nm, (_h ([string]$_.SourceSpecialty)),
            (_h (("{0}, {1}" -f $_.City, $_.State).Trim(', ').Trim())),
            ('{0:N0}' -f $_.SharedPatients), $_.PctOfVolume, $_.CumulativePct, $_.DistanceMiles, $extra, $backCell
    }) -join "`n"
    $srcTableNote = (@(
        $(if (@($a.Sources).Count -gt 25) { "Showing the top 25 of $('{0:N0}' -f @($a.Sources).Count) sources — the full table is in the CSV saved beside this report." })
        $(if ($hasBack) { 'Back = patients this practice shared ONWARD to that same source in the same year: in ≈ back with a long lag is co-occurring care (labs, hospitals); in far above back with a short lag is winnable referral flow.' })
        $(if ($anyRisk) { '&#9888; = no longer Medicare order-&-refer eligible on TODAY''s Order & Referring roster (retired, deactivated, or dis-enrolled) — future Medicare referrals from them would deny.' })
    ) | Where-Object { $_ }) -join ' '
    $waitTh = if ($a.IsHop) { '<th class="num">Avg lag (days)</th>' } else { '' }
    $allNotes = @($a.Notes) + $(if ($tr) { @('') + @($tr.Notes) } else { @() })
    $notesHtml = (@($allNotes) | Where-Object { $_ } | ForEach-Object { '<li>' + (_h ([string]$_)) + '</li>' }) -join "`n"
    $generated = (Get-Date).ToString('MMMM d, yyyy')

    $html = @"
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Referral Source Analysis — $(_h $a.Practice.Name)</title>
__RM_LEAFLET_CSS__
__RM_LEAFLET_JS__
<style>
  /* Corporate palette: deep navy ink, bold cobalt accent, amber highlight.
     Sharp edges (2px radii), strong contrast, responsive from phone to
     desktop - wide tables scroll inside their card instead of breaking
     the page. */
  :root { --ink:#0f1f33; --sub:#4e5d6e; --line:#c9d3de; --accent:#0f5cad;
          --accent-dark:#0a3f78; --amber:#b45309; --bg:#e9edf2; }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:"Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif; }
  .wrap { max-width:1120px; margin:0 auto; padding:0 22px 36px; }
  header { display:flex; flex-wrap:wrap; align-items:center; gap:10px 16px;
           background:linear-gradient(135deg, var(--accent-dark), var(--accent));
           margin:0 -22px 14px; padding:20px 26px 18px;
           border-bottom:4px solid var(--amber); }
  header h1 { margin:0; color:#fff; font-size:23px; font-weight:700; letter-spacing:-.2px; }
  .badge { background:#fff; color:var(--accent-dark); font-size:11.5px; font-weight:700;
           padding:4px 12px; border-radius:2px; white-space:nowrap; letter-spacing:.4px; }
  .sub { color:var(--sub); font-size:13.5px; margin:2px 0 16px; }
  .stats { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 18px; }
  .stat { background:#fff; border:1px solid var(--line); border-top:3px solid var(--accent);
          border-radius:2px; padding:12px 18px; min-width:150px; flex:1 1 150px;
          box-shadow:0 1px 3px rgba(10,30,55,.10); }
  .stat b { display:block; font-size:23px; font-weight:700; letter-spacing:-.4px; color:var(--accent-dark);
            font-variant-numeric:tabular-nums; }
  .stat span { font-size:10.5px; color:var(--sub); text-transform:uppercase; letter-spacing:.7px; font-weight:600; }
  .stat.warn { border-top-color:#c0390f; }
  .stat.warn b { color:#c0390f; }
  .card { background:#fff; border:1px solid var(--line); border-radius:2px;
          box-shadow:0 1px 3px rgba(10,30,55,.10); margin-bottom:18px; padding:0 0 8px;
          overflow-x:auto; }
  .card h2 { margin:0; padding:14px 18px 10px; font-size:13.5px; font-weight:700;
             text-transform:uppercase; letter-spacing:.8px; color:var(--accent-dark);
             border-bottom:2px solid var(--accent); }
  .card .body { padding:10px 18px 8px; }
  .duo { display:flex; flex-wrap:wrap; gap:18px; }
  .duo > div { flex:1 1 460px; min-width:0; }
  /* Chart SVGs only — scoped to .body so Leaflet's attribute-sized overlay
     pane is untouched (a global svg rule collapsed it to 0x0 and made every
     map circle invisible; found by probing the rendered geometry). */
  .body svg { width:100%; height:auto; display:block; max-width:100%; }
  .bar { fill:var(--accent); }
  .blbl { font-size:12.5px; fill:#22364d; font-weight:600; }
  .bval { font-size:11.5px; fill:#44566b; }
  .dbig { font-size:26px; font-weight:700; fill:#0f1f33; }
  .dsm { font-size:11px; fill:#44566b; }
  .grid { stroke:#dfe6ed; stroke-width:1; }
  .axlbl { font-size:10.5px; fill:#5f7186; }
  .curve { fill:none; stroke:var(--accent); stroke-width:3; }
  .curve2 { fill:none; stroke:var(--amber); stroke-width:2.5; }
  .dot2 { fill:var(--amber); }
  .lbl2 { font-size:10.5px; fill:#8a5410; font-weight:600; }
  .bin { font-size:11.5px; fill:#ffffff; font-weight:700; }
  .segKept { fill:#0f5cad; } .segNew { fill:#5a92c9; } .segLost { fill:#c3cdd8; }
  .sub2 { margin:2px 0 0; padding:8px 18px 0; font-size:12.5px; font-weight:700; color:#22364d; }
  td.up { color:#116b3f; font-weight:700; }
  td.down { color:#c0390f; font-weight:700; }
  .findings { font-size:13.5px; line-height:1.7; margin:2px 0 6px; padding-left:22px; }
  .findings li { margin-bottom:6px; }
  .findings li::marker { color:var(--accent); font-weight:700; }
  .empty { font-size:13.5px; line-height:1.6; margin:2px 0 10px; color:#22364d; }
  table { border-collapse:collapse; width:100%; font-size:12.6px; }
  th, td { border-top:1px solid var(--line); padding:7px 12px; text-align:left; }
  th { background:var(--accent-dark); color:#fff; font-weight:700; font-size:10.5px;
       text-transform:uppercase; letter-spacing:.6px; border-top:none; white-space:nowrap; }
  tr:nth-child(even) td { background:#f2f6fa; }
  tr.you td { background:#dcebf8; font-weight:700; border-top:2px solid var(--accent); border-bottom:2px solid var(--accent); }
  .youtag { background:var(--amber); color:#fff; font-size:9.5px; font-weight:700;
            padding:2px 7px; border-radius:2px; vertical-align:1px; letter-spacing:.6px; }
  .proftbl th { text-align:left; white-space:nowrap; width:220px; background:#f2f6fa; color:#22364d;
                font-size:11px; vertical-align:top; padding:8px 12px; }
  .proftbl td { font-size:12.5px; padding:8px 12px; }
  .chain { color:var(--amber); font-weight:700; cursor:help; }
  .barmuted { fill:#96abc0; }
  #rm-map { height:52vh; min-height:380px; }
  .offline { padding:9px 14px; background:#fff3cd; color:#6b4e0e; font-size:12.5px;
             border-bottom:1px solid #e7d59a; display:none; }
  .prac-pin { width:22px; height:22px; border-radius:50%; background:#c62828;
              border:3px solid #fff; box-shadow:0 1px 6px rgba(0,0,0,.45); }
  .legend { background:#fff; padding:9px 12px; border-radius:2px;
            box-shadow:0 1px 5px rgba(0,0,0,.25); font-size:12px; line-height:19px; }
  .legend i { width:12px; height:12px; display:inline-block; border-radius:50%;
              margin-right:6px; vertical-align:-2px; }
  .maptools { display:flex; flex-wrap:wrap; align-items:center; gap:8px;
              padding:9px 16px; border-bottom:1px solid var(--line); background:#f2f6fa; }
  .mtlabel { font-size:11px; color:var(--sub); text-transform:uppercase; letter-spacing:.6px; font-weight:700; }
  .mtbtn { font:inherit; font-size:12.5px; padding:5px 12px; border-radius:2px; cursor:pointer;
           border:1px solid var(--line); background:#fff; color:#22364d; font-weight:600; }
  td.mono { font-variant-numeric:tabular-nums; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .mtbtn.active { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:700; }
  .mtchk { font-size:12.5px; color:#22364d; display:inline-flex; align-items:center; gap:5px; }
  .note { color:var(--sub); font-size:12px; }
  .tablenote { padding:8px 18px 10px; color:var(--sub); font-size:12px; }
  details { margin:0; } summary { cursor:pointer; padding:14px 18px; font-size:13.5px; font-weight:700;
            text-transform:uppercase; letter-spacing:.8px; color:var(--accent-dark); }
  .notes { font-size:12px; color:#435364; line-height:1.6; margin:0; padding:0 22px 14px 36px; }
  .notes li { margin-bottom:5px; }
  footer { color:var(--sub); font-size:11.5px; margin-top:6px; }
  /* Tablet */
  @media (max-width: 900px) {
    .wrap { padding:0 14px 28px; }
    header { margin:0 -14px 12px; padding:16px 18px 14px; }
    .duo > div { flex:1 1 100%; }
    .proftbl th { width:150px; white-space:normal; }
  }
  /* Phone */
  @media (max-width: 620px) {
    header h1 { font-size:19px; }
    .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .stat { min-width:0; padding:10px 12px; }
    .stat b { font-size:19px; }
    table { font-size:11.8px; }
    th, td { padding:6px 8px; }
    #rm-map { height:60vh; min-height:300px; }
    .card h2 { font-size:12.5px; }
  }
  @media print { body { background:#fff; } .card { box-shadow:none; overflow:visible; }
                 header { background:var(--accent-dark) !important; -webkit-print-color-adjust:exact; } }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Referral Source Analysis</h1>
  <span class="badge">$(_h $a.Label)</span>
</header>
<p class="sub"><b>$(_h $a.Practice.Name)</b> &mdash; NPI $($a.Npi)$(if ($a.PSObject.Properties['NpiCount'] -and $a.NpiCount -gt 1) { " (+$($a.NpiCount - 1) affiliated NPI$(if ($a.NpiCount -gt 2) { 's' }) combined)" }), $(_h ("$($a.Practice.City), $($a.Practice.State) $($a.Practice.Zip)")).
Inbound Medicare shared-patient volume, $($a.Year).</p>
<div class="stats">
  <div class="stat"><b>$('{0:N0}' -f $a.TotalPatients)</b><span>Measured patient base</span></div>
$(if ($therPat -gt 0) { '  <div class="stat"><b>' + ('{0:N0}' -f $refPat) + '</b><span>From external referrers</span></div>' })
$(if ($therPat -gt 0) { '  <div class="stat"><b>' + ('{0:N0}' -f $therPat) + '</b><span>Own-therapist volume</span></div>' })
  <div class="stat"><b>$('{0:N0}' -f $a.SourceCount)</b><span>External referral sources</span></div>
  <div class="stat"><b>$(if ($hasVolume) { "$($a.Top1Pct)%" } else { '&mdash;' })</b><span>From top source</span></div>
  <div class="stat"><b>$(if ($hasVolume) { "$($a.Top5Pct)%" } else { '&mdash;' })</b><span>Top-5 dependence</span></div>
  <div class="stat$(if ($hasVolume -and $a.HHI -ge 2500) { ' warn' })"><b>$(if ($hasVolume) { '{0:N0}' -f $a.HHI } else { '&mdash;' })</b><span>Concentration (HHI)</span></div>
$(if ($comp -and $comp.Rank) { '  <div class="stat"><b>#' + $comp.Rank + ' of ' + ('{0:N0}' -f $comp.ProviderCount) + '</b><span>Rank within ' + $comp.RadiusMiles + ' mi</span></div>' })
</div>
$profileHtml
<div class="card"><h2>Key findings</h2><div class="body"><ul class="findings">
$findingsHtml
</ul></div></div>
$(if ($hasVolume) { @"
<div class="card"><h2>Top referral sources</h2><div class="body">$barSvg</div></div>
<div class="duo">
  <div class="card"><h2>Specialty mix of the referral base</h2><div class="body">$donutSvg</div></div>
  <div class="card"><h2>Concentration curve (cumulative share)</h2><div class="body">$paretoSvg</div></div>
</div>
<div class="duo">
  <div class="card"><h2>Volume by distance from the practice</h2><div class="body">$distSvg</div></div>
  $(if ($waitSvg) { '<div class="card"><h2>Volume by referral lag (days from source visit)</h2><div class="body">' + $waitSvg + '</div></div>' })
</div>
$geoCardHtml
"@ } else { @"
<div class="card"><h2>No measured referral volume for this practice</h2><div class="body">
<p class="empty">The $($a.Year) file records <b>no inbound shared-patient pairs</b> for this NPI, so there is nothing to chart:
source, specialty, distance and referral-lag breakdowns are omitted rather than drawn empty.</p>
<p class="empty">This does <b>not</b> mean the practice received no referrals. The common explanations are:</p>
<ul class="findings">
  <li><b>The 11-patient privacy floor.</b> Any source that shared fewer than 11 distinct Medicare patients with this practice during the year is removed from the data at the source. A smaller practice can have its entire referral base fall below that line.</li>
  <li><b>Volume billed under a different NPI.</b> Claims may run through the therapists' individual NPIs rather than the organization NPI (or the reverse). Re-run the analysis with all of the practice's NPIs entered together to combine them.</li>
  <li><b>Medicare Fee-for-Service only.</b> Medicare Advantage, commercial, and self-pay patients are not in this data at all.</li>
  <li><b>The NPI was not yet active</b> in $($a.Year), or the practice enumerated a newer NPI since.</li>
</ul>
<p class="empty">The competitive landscape below is still measured and useful: it shows which providers in the same area <i>do</i> carry measured volume.</p>
</div></div>
"@ })
$outHtml
$trendHtml
$compHtml
$outreachHtml
$(if ($hasVolume) { @"
<div class="card">
  <h2>Source detail</h2>
  <table>
    <tr><th class="num">#</th><th>NPI</th><th>Source</th><th>Specialty</th><th>Location</th>
        <th class="num">Patients</th>$(if ($hasBack) { '<th class="num">Back</th>' })<th class="num">% of vol</th><th class="num">Cum %</th><th class="num">Miles</th>$waitTh</tr>
    $srcRowsHtml
  </table>
  $(if ($srcTableNote) { "<div class='tablenote'>$srcTableNote</div>" })
</div>
"@ })
$(if (@($therList).Count) { @"
<div class="card">
  <h2>Practice therapists &mdash; the practice's own clinicians in the raw file</h2>
  <div class="body"><p>These individual PT/OT/SLP NPIs appear in the raw shared-patient file as "sources" of this practice. That is the practice's own caseload arriving through its clinicians (therapist-and-organization co-billing), <b>not external referrals</b>, so their $('{0:N0}' -f $therPat) patients are counted in the measured patient base above and kept out of the referral ranking. "VERIFIED" means the clinician is on this practice's own Medicare Care Compare roster today.</p></div>
  <table>
    <tr><th>NPI</th><th>Clinician</th><th>Specialty</th><th class="num">Patients</th><th>Roster check</th></tr>
    $(@($therList | ForEach-Object {
        '<tr><td class="mono">{0}</td><td>{1}</td><td>{2}</td><td class="num">{3}</td><td>{4}</td></tr>' -f
            $_.SourceNPI, (_h ([string]$_.SourceName)), (_h ([string]$_.SourceSpecialty)),
            ('{0:N0}' -f [int]$_.SharedPatients), (_h ([string]$_.Staff))
    }) -join "`n")
  </table>
</div>
"@ })
$(if (@($rosterList).Count) {
    $rSortProps = @(
        @{Expression = { if ($rosterVol.ContainsKey([string]$_.NPI)) { -($rosterVol[[string]$_.NPI].In + $rosterVol[[string]$_.NPI].Out) } else { 0 } }}
        @{Expression = 'Clinician'})
    $rShown = @($rosterList | Sort-Object -Property $rSortProps | Select-Object -First 60)
    $rRows = (@($rShown) | ForEach-Object {
        $v = if ($rosterVol.ContainsKey([string]$_.NPI)) { $rosterVol[[string]$_.NPI] } else { $null }
        $volCell = if ($v) {
            (@($(if ($v.In) { "$('{0:N0}' -f $v.In) in" }), $(if ($v.Out) { "$('{0:N0}' -f $v.Out) out" })) | Where-Object { $_ }) -join ' + '
        } else { '&mdash;' }
        '<tr><td class="mono">{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td class="num">{4}</td></tr>' -f
            $_.NPI, (_h ([string]$_.Clinician)), (_h ([string]$_.Specialty)),
            (_h (("$($_.City), $($_.State)").Trim(', ').Trim())), $volCell
    }) -join "`n"
    @"
<div class="card">
  <h2>Care Compare roster &mdash; clinicians billing under this practice today</h2>
  <div class="body"><p>Medicare Care Compare lists $('{0:N0}' -f @($rosterList).Count) clinician$(if (@($rosterList).Count -ne 1) { 's' }) under this practice today (matched by practice name, then expanded to everyone sharing the same group-enrollment id). The last column shows each clinician's measured volume in this year's shared-patient file &mdash; "in" is caseload arriving through that clinician, "out" is continued care under them after an organization visit; a dash means every pair fell under the 11-patient privacy floor or the clinician bills only through the group NPI. For the practice's own staff the two directions largely overlap &mdash; the same same-day co-billed patients counted from each side &mdash; so "in" and "out" must not be added together.</p></div>
  <table>
    <tr><th>NPI</th><th>Clinician</th><th>Specialty</th><th>Location</th><th class="num">In this year's data</th></tr>
    $rRows
  </table>
  <div class="tablenote">$(if (@($rosterList).Count -gt 60) { "Showing 60 of $('{0:N0}' -f @($rosterList).Count) roster clinicians (highest measured volume first). " })This is TODAY's roster: clinicians who left the practice are absent even when their historical volume appears above, and cash-pay or non-Medicare clinicians never appear here.</div>
</div>
"@ })
<div class="card">
  <details>
    <summary>Methodology &amp; limitations</summary>
    <ul class="notes">
      $notesHtml
    </ul>
  </details>
</div>
<footer>Generated $generated by the Medicare Order &amp; Referring Tracker &middot; ZIP centroids: US Census 2023 ZCTA gazetteer &middot; Provider identities: NPPES registry</footer>
</div>
</body>
</html>
"@
    # LITERAL replacement — the Leaflet bundle must never be interpolated.
    $html = $html.Replace('__RM_LEAFLET_CSS__', $lfCss).Replace('__RM_LEAFLET_JS__', $lfJs)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $enc = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllText(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path), $html, $enc)
    [pscustomobject]@{ Path = $Path; Sources = @($a.Sources).Count; TotalPatients = $a.TotalPatients }
}

# ---------------------------------------------------------------------------
# Supplemental open data (data.cms.gov, keyless): county Medicare market
# size + MA share, and real billed therapy services per provider. Both are
# cosmetic ENRICHMENT — a failed fetch degrades to a note, never an error.
# ---------------------------------------------------------------------------

$script:RmZctaCounty = $null
function Get-RmZctaCountyFips([string]$Zip, [string]$CrosswalkPath) {
    # ZIP -> county FIPS via the bundled US Census 2020 ZCTA-county
    # relationship table (largest-land-overlap county per ZCTA).
    if ($null -eq $script:RmZctaCounty -or $CrosswalkPath) {
        $path = if ($CrosswalkPath) { $CrosswalkPath } else { Join-Path $PSScriptRoot 'zcta-county.csv' }
        $t = @{}
        foreach ($line in [System.IO.File]::ReadLines($path)) {
            $p = $line.Split(',')
            if ($p.Count -ge 2 -and $p[0] -match '^\d{5}$') { $t[$p[0]] = $p[1] }
        }
        if ($CrosswalkPath) { return $(if ($t.ContainsKey($Zip)) { $t[$Zip] } else { $null }) }
        $script:RmZctaCounty = $t
    }
    if ($script:RmZctaCounty.ContainsKey($Zip)) { $script:RmZctaCounty[$Zip] } else { $null }
}

function Invoke-RmCmsApi([string]$DatasetId, [string]$Query) {
    $url = '{0}/{1}/data?{2}' -f $script:RmConfig.CmsApiBase, $DatasetId, $Query
    $delays = @(0, 2, 5); $lastMsg = ''
    foreach ($delay in $delays) {
        if ($delay) { Start-Sleep -Seconds $delay }
        try {
            $resp = Invoke-RestMethod -Uri $url -TimeoutSec 60 -ErrorAction Stop
            # Newer PowerShell emits a JSON array as ONE non-enumerated
            # object; pipe it so callers' @(...) reliably sees N row items
            # (this exact wrap made row filters see nothing in testing).
            return @($resp | ForEach-Object { $_ })
        } catch { $lastMsg = $_.Exception.Message }
    }
    throw "The CMS open-data API (data.cms.gov) could not be reached after $($delays.Count) attempts. Details: $lastMsg"
}

# Small keyed JSON caches (county market, billed-services profiles): one
# reader and one atomic writer instead of a copy in each consumer. Purely
# cosmetic caches - unreadable or unwritable files degrade to a re-fetch.
function Read-RmJsonCache([string]$Path) {
    $cache = @{}
    if (Test-Path -LiteralPath $Path) {
        try { $j = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
              foreach ($p in $j.PSObject.Properties) { $cache[$p.Name] = $p.Value } } catch {}
    }
    $cache
}

function Write-RmJsonCache([string]$Path, [hashtable]$Cache) {
    try {
        New-Item -ItemType Directory -Path $script:RmConfig.DataDir -Force | Out-Null
        $ctmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        $Cache | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ctmp -Encoding UTF8
        Move-Item -LiteralPath $ctmp -Destination $Path -Force
    } catch {}
}

function Get-RmCountyMarket {
    <#
    .SYNOPSIS
      County Medicare market context for a practice ZIP, from CMS's Medicare
      Monthly Enrollment dataset: total beneficiaries, Original-Medicare
      (FFS) vs Medicare Advantage split, latest full year. Cached on disk.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidatePattern('^\d{5}$')][string]$Zip,
        [string]$CrosswalkPath
    )
    $fips = Get-RmZctaCountyFips $Zip $CrosswalkPath
    if (-not $fips) { return $null }
    $cachePath = Join-Path $script:RmConfig.DataDir 'market-cache.json'
    $cache = Read-RmJsonCache $cachePath
    if ($cache.ContainsKey($fips)) { return $cache[$fips] }
    # Local enrollment index first (works with no internet); the API is the
    # fallback when the user has not imported the file.
    $rows = @()
    $enrollIdx = Get-RmEnrollmentIndexPath
    if (Test-Path -LiteralPath $enrollIdx) {
        $want = New-Object 'System.Collections.Generic.HashSet[string]'
        [void]$want.Add($fips)
        foreach ($line in @([RmEngine]::ScanRosterIndex($enrollIdx, $want, 0))) {
            $f = $line.Split('|')
            if ($f.Count -lt 8 -or $f[2] -ne 'Year') { continue }
            $rows += [pscustomobject]@{
                YEAR = $f[1]; BENE_COUNTY_DESC = $f[3]; BENE_STATE_ABRVTN = $f[4]
                TOT_BENES = $f[5]; ORGNL_MDCR_BENES = $f[6]; MA_AND_OTH_BENES = $f[7] }
        }
    }
    if (-not $rows.Count) {
        $rows = @(Invoke-RmCmsApi 'd7fabe1e-d19b-4333-9eff-e80e0643f2fd' `
            ('filter[BENE_FIPS_CD]={0}&filter[MONTH]=Year&size=50' -f $fips))
    }
    $rows = @($rows | Where-Object { [string]$_.TOT_BENES -match '^\d+$' } | Sort-Object { [int]$_.YEAR })
    if (-not $rows.Count) { return $null }
    $r = $rows[$rows.Count - 1]
    $tot = [int]$r.TOT_BENES
    $ma = if (([string]$r.MA_AND_OTH_BENES) -match '^\d+$') { [int]$r.MA_AND_OTH_BENES } else { 0 }
    $ffs = if (([string]$r.ORGNL_MDCR_BENES) -match '^\d+$') { [int]$r.ORGNL_MDCR_BENES } else { 0 }
    # Every year's FFS count rides along so a caller can divide data-year
    # volume by SAME-year enrollment instead of the latest year's (FFS shrinks
    # as MA grows, so a mismatched denominator biases the rate). Stored as an
    # object, not a hashtable, so the disk-cached (JSON) and fresh shapes read
    # identically via .PSObject.Properties.
    $fy = [ordered]@{}
    foreach ($row in $rows) {
        if (([string]$row.ORGNL_MDCR_BENES) -match '^\d+$') { $fy[[string][int]$row.YEAR] = [int]$row.ORGNL_MDCR_BENES }
    }
    $m = [pscustomobject]@{
        Fips = $fips; County = [string]$r.BENE_COUNTY_DESC; State = [string]$r.BENE_STATE_ABRVTN
        Year = [int]$r.YEAR; TotalBenes = $tot; FfsBenes = $ffs; MaBenes = $ma
        MaPct = if ($tot -gt 0) { [math]::Round(100.0 * $ma / $tot, 1) } else { 0 }
        FfsByYear = [pscustomobject]$fy
    }
    $cache[$fips] = $m
    Write-RmJsonCache $cachePath $cache
    $m
}

function Get-RmServiceProfile {
    <#
    .SYNOPSIS
      Real billed Medicare Part B therapy activity for one or more NPIs,
      from CMS's Physician & Other Practitioners (by Provider and Service)
      dataset — actual claims, no 11-patient pair floor. Therapy = HCPCS
      97xxx (PT/OT) and 92xxx speech codes. Organizations that bill under
      their therapists' individual NPIs legitimately show nothing here.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidateCount(1, 60)][ValidatePattern('^\d{10}$')][string[]]$Npi)
    $cachePath = Join-Path $script:RmConfig.DataDir 'services-cache.json'
    $cache = Read-RmJsonCache $cachePath
    $dirty = $false
    $out = @{}
    foreach ($n in @($Npi | Sort-Object -Unique)) {
        if ($cache.ContainsKey($n)) { $out[$n] = $cache[$n]; continue }
        $rows = @(Invoke-RmCmsApi '92396110-2aed-4d63-a6a2-5d6207d46a29' `
            ('filter[Rndrng_NPI]={0}&size=200' -f $n))
        $svc = 0; $maxBene = 0; $codes = 0; $type = ''
        foreach ($r in $rows) {
            $code = [string]$r.HCPCS_Cd
            if ($code -notmatch '^(97|92)') { continue }
            $codes++
            $svc += [int][double]$r.Tot_Srvcs
            if ([int]$r.Tot_Benes -gt $maxBene) { $maxBene = [int]$r.Tot_Benes }
            if (-not $type) { $type = [string]$r.Rndrng_Prvdr_Type }
        }
        $p = [pscustomobject]@{
            Npi = $n; TherapyServices = $svc; TherapyCodes = $codes
            MinDistinctPatients = $maxBene   # max single-code bene count = a FLOOR, benes overlap across codes
            ClaimsSpecialty = $type
            HasAnyClaims = ($rows.Count -gt 0)
        }
        $cache[$n] = $p; $out[$n] = $p; $dirty = $true
    }
    if ($dirty) { Write-RmJsonCache $cachePath $cache }
    $out
}

# ---------------------------------------------------------------------------
# Local rosters: NPPES bulk file (offline provider lookups) and Care Compare
# DAC file (clinician -> practice-group membership, for suggesting which
# NPIs to combine). Both import once to compact pipe-delimited indexes.
# ---------------------------------------------------------------------------

function Get-RmNppesIndexPath { Join-Path $script:RmConfig.DataDir 'nppes-index.psv' }
function Get-RmNppesLocIndexPath { Join-Path $script:RmConfig.DataDir 'nppes-locations.psv' }
function Get-RmNppesOtherNamePath { Join-Path $script:RmConfig.DataDir 'nppes-othernames.psv' }
function Get-RmChainIndexPath { Join-Path $script:RmConfig.DataDir 'chain-index.psv' }

# A single outpatient therapy SITE rarely exceeds a few hundred distinct
# referring providers - a large, busy one-location practice measured 379.
# An NPI far above that is a billing NPI covering many sites, so its volume
# must not be read as one address's business. Verified case: IvyRehab's
# Hoboken-registered NPI carries 194,516 patients from 4,204 sources.
$script:RmSingleSiteSourceCeiling = 750

# A national chain registers the SAME legal name over and over - one
# organization NPI per clinic. Counting those NPIs is therefore a direct
# read on "is this a chain?". Calibrated on the real NPPES bulk file over
# organizations carrying an in-scope PT/OT/speech taxonomy (78,783 names):
# 91.8% hold exactly ONE org NPI and can never be flagged, 6.4% hold 2-3,
# and >3 selects just 1.79% of names - 642 of them spanning two or more
# states. Verified: Ivy Rehab Network Inc 8 NPIs / 7 cities, Ivy Rehab SE
# PT LLC 14 / 9, ATI Holdings LLC 67 / 46, Athletico Ltd 423 / 308.
$script:RmChainNpiThreshold = 3
$script:RmChainIdx = $null

# nameKey -> @{ Npis; Cities }, built once from the local NPPES bulk index.
# Absent index = no flag anywhere (never a guess).
function Get-RmChainIndex {
    if ($null -ne $script:RmChainIdx) { return $script:RmChainIdx }
    $t = $null
    $p = Get-RmNppesIndexPath
    if (Test-Path -LiteralPath $p) {
        $cp = Get-RmChainIndexPath
        # Rebuild whenever the derived file is missing or older than the
        # index it comes from, so a fresh NPPES import can never be read
        # through last month's chain table.
        $stale = -not (Test-Path -LiteralPath $cp)
        if (-not $stale) {
            $stale = (Get-Item -LiteralPath $cp).LastWriteTimeUtc -lt (Get-Item -LiteralPath $p).LastWriteTimeUtc
        }
        if (-not $stale) {
            # A table built under older name-normalisation rules holds keys
            # that no longer match; the stamp catches that, timestamps cannot.
            $first = ''
            try { foreach ($ln in [System.IO.File]::ReadLines($cp)) { $first = $ln; break } } catch { }
            $stale = ($first -ne [RmEngine]::ChainIndexVersion)
        }
        if ($stale) {
            Write-Verbose 'Building the chain table from the NPPES index (one time)...'
            $tmp = $cp + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
            [void][RmEngine]::BuildChainIndex($p, $tmp)
            Move-Item -LiteralPath $tmp -Destination $cp -Force
        }
        $t = [RmEngine]::LoadChainIndex($cp)
    }
    if ($null -eq $t) { $t = New-Object 'System.Collections.Generic.Dictionary[string,int[]]' }
    $script:RmChainIdx = $t
    $t
}

# '*' when this organization name is registered by more than
# $script:RmChainNpiThreshold organization NPIs; '' otherwise (and '' when
# the local index is missing, or the row is an individual therapist).
function Get-RmChainMark([string]$Name) {
    if (-not $Name) { return '' }
    $idx = Get-RmChainIndex
    if (-not $idx.Count) { return '' }
    $k = Get-RmOrgNameKey $Name
    if (-not $k -or -not $idx.ContainsKey($k)) { return '' }
    if ($idx[$k][0] -gt $script:RmChainNpiThreshold) { '*' } else { '' }
}

function Get-RmChainDetail([string]$Name) {
    $idx = Get-RmChainIndex
    $k = Get-RmOrgNameKey $Name
    if (-not $idx.Count -or -not $k -or -not $idx.ContainsKey($k)) {
        return [pscustomobject]@{ Npis = 0; Cities = 0; IsChain = $false }
    }
    $e = $idx[$k]
    [pscustomobject]@{
        Npis = $e[0]; Cities = $e[1]
        IsChain = ($e[0] -gt $script:RmChainNpiThreshold)
    }
}

# Some groups give every clinic its OWN legal name, so counting identical
# names cannot see them. Real case: Advanced Training and Rehab in St Louis
# enrolls ATR JUSTIN LLC, ATR RYAN LLC, ATR JEFF LLC, ATR CHRIS LLC, ATR
# MORGAN LLC, ATR JOHN LLC, ATR-TC LLC, ATR HAND THERAPY LLC - eight
# separate names, so eight modest rows instead of one large one, and the
# practice looks far smaller than it is.
#
# Within ONE result set, organizations sharing a distinctive LEADING WORD
# are therefore surfaced as a possible single company. This is a prompt to
# look, never an assertion: nothing is merged and no volume is combined.
$script:RmSiblingMinGroup = 3
# Words that lead the names of unrelated practices everywhere, so a shared
# one means nothing. Drawn from a live 63101 + 30mi sweep, where grouping on
# them produced junk clusters ('PHYSICAL' pulled 10 unrelated practices
# together) while the real clusters - ATR, EMPOWERME, FOX, LEGACY - all lead
# with a distinctive word.
$script:RmGenericLeadWords = @{}
foreach ($w in @(
    'PHYSICAL', 'THERAPY', 'THERAPIES', 'REHAB', 'REHABILITATION', 'SPORTS', 'SPORT',
    'MEDICAL', 'MEDICINE', 'HEALTH', 'HEALTHCARE', 'CLINIC', 'CENTER', 'CENTRE', 'CENTERS',
    'ORTHOPEDIC', 'ORTHOPAEDIC', 'OCCUPATIONAL', 'SPEECH', 'HAND', 'SPINE', 'PEDIATRIC',
    'PEDIATRICS', 'WELLNESS', 'FAMILY', 'COMMUNITY', 'REGIONAL', 'MEMORIAL', 'UNIVERSITY',
    'HOSPITAL', 'ASSOCIATES', 'PARTNERS', 'GROUP', 'SERVICES', 'SOLUTIONS', 'CARE',
    'NORTH', 'SOUTH', 'EAST', 'WEST', 'CENTRAL', 'GREATER', 'VALLEY', 'LAKE', 'RIVER', 'PARK',
    'ADVANCED', 'PREMIER', 'PROFESSIONAL', 'PROGRESSIVE', 'COMPLETE', 'TOTAL', 'INTEGRATED',
    'INNOVATIVE', 'QUALITY', 'PRECISION', 'DYNAMIC', 'ACTIVE', 'OPTIMAL', 'SUPERIOR',
    'AMERICAN', 'NATIONAL', 'UNITED', 'FIRST', 'NEW', 'ALL', 'PRO', 'BACK', 'BODY', 'MOTION',
    'PERFORMANCE', 'RECOVERY', 'RESTORE', 'RENEW', 'BALANCE', 'STRENGTH',
    'PAIN', 'INJURY', 'MOBILITY', 'MOVEMENT', 'FUNCTION', 'FUNCTIONAL', 'KIDS', 'CHILDRENS',
    'COMPREHENSIVE', 'ASSOCIATED', 'GENERAL', 'MIDWEST', 'METRO', 'METROPOLITAN', 'SUBURBAN',
    'COMMUNITY', 'UNIVERSAL', 'ALLIED', 'APEX', 'SUMMIT', 'PINNACLE',
    'SENIOR', 'HOME', 'MOBILE', 'SPORTSMED', 'PHYSIO', 'PHYSIOTHERAPY',
    # A state name leads unrelated practices in that state; too weak to group on.
    'ALABAMA', 'ALASKA', 'ARIZONA', 'ARKANSAS', 'CALIFORNIA', 'COLORADO', 'CONNECTICUT',
    'DELAWARE', 'FLORIDA', 'GEORGIA', 'HAWAII', 'IDAHO', 'ILLINOIS', 'INDIANA', 'IOWA',
    'KANSAS', 'KENTUCKY', 'LOUISIANA', 'MAINE', 'MARYLAND', 'MASSACHUSETTS', 'MICHIGAN',
    'MINNESOTA', 'MISSISSIPPI', 'MISSOURI', 'MONTANA', 'NEBRASKA', 'NEVADA', 'HAMPSHIRE',
    'JERSEY', 'MEXICO', 'YORK', 'CAROLINA', 'DAKOTA', 'OHIO', 'OKLAHOMA', 'OREGON',
    'PENNSYLVANIA', 'RHODE', 'TENNESSEE', 'TEXAS', 'UTAH', 'VERMONT', 'VIRGINIA',
    'WASHINGTON', 'WISCONSIN', 'WYOMING')) { $script:RmGenericLeadWords[$w] = $true }

function Get-RmNameSiblingGroups {
    param([object[]]$Rows, [string]$NameProperty = 'Name', [string]$TypeProperty = 'Type')
    $byLead = @{}
    foreach ($r in @($Rows)) {
        if ($TypeProperty -and $r.PSObject.Properties[$TypeProperty] -and $r.$TypeProperty -ne 'Organization') { continue }
        $nm = [string]$r.$NameProperty
        $k = Get-RmOrgNameKey $nm
        if (-not $k) { continue }
        $lead = @($k -split ' ')[0]
        # Two characters matches far too much; a purely numeric lead is
        # noise; an industry-generic lead groups strangers.
        if ($lead.Length -lt 3 -or $lead -match '^\d+$') { continue }
        if ($script:RmGenericLeadWords.ContainsKey($lead)) { continue }
        if (-not $byLead.ContainsKey($lead)) { $byLead[$lead] = New-Object System.Collections.Generic.List[object] }
        $byLead[$lead].Add($r)
    }
    $out = foreach ($kv in $byLead.GetEnumerator()) {
        # Distinct NAMES, not rows: one company holding two NPIs under the
        # same name is already covered by the Chain flag.
        $names = @($kv.Value | ForEach-Object { Get-RmOrgNameKey ([string]$_.$NameProperty) } | Select-Object -Unique)
        if ($names.Count -lt $script:RmSiblingMinGroup) { continue }
        $vol = 0
        foreach ($r in $kv.Value) { if ($r.PSObject.Properties['SharedPatients']) { $vol += [int]$r.SharedPatients } }
        [pscustomobject]@{
            LeadWord = $kv.Key
            Organizations = $names.Count
            CombinedPatients = $vol
            Names = @($kv.Value | ForEach-Object { [string]$_.$NameProperty } | Select-Object -Unique | Sort-Object)
        }
    }
    @($out | Sort-Object CombinedPatients -Descending)
}

# The legend that must travel with any table carrying a Chain column.
function Get-RmChainNote {
    ("CHAIN FLAG (*): an asterisk in the Chain column means this organization NAME is registered by more than " +
     "$($script:RmChainNpiThreshold) organization NPIs in NPPES - the signature of a multi-site company (one org NPI per clinic), " +
     "not an independent practice. Its measured volume is that ONE NPI's, so it is a slice of the company, and the company's " +
     "local footprint is larger than the row suggests. Break it apart on the Multi-site chains tab. Calibrated on the full " +
     "NPPES file: 91.8% of outpatient PT/OT/speech organizations hold exactly one org NPI and are never flagged; the flag " +
     "selects 1.79% of names. A blank flag means 'not flagged', which needs the local NPPES index to be meaningful. " +
     "ONE LIMIT: the flag counts a NAME, so a GENERIC name shared by unrelated organizations also carries it - " +
     "'MEMORIAL HOSPITAL' shows 151 org NPIs across 45 cities because many separate hospitals use that name, not because " +
     "they are one company. Read the flag as 'this exact name is registered many times', which for a distinctive brand " +
     "means a chain and for a generic name means check before concluding.")
}

$script:RmLocCounts = $null
$script:RmLocZips = $null
function Initialize-RmLocTables {
    # One pass loads both views of nppes-locations.psv: NPI -> site count
    # (multi-site flags) and NPI -> 'zip1;zip2' (secondary-site sweeps).
    # Old-format files (NPI|count, no third field) still feed the counts;
    # the ZIP table just stays empty, which switches the sweep feature off.
    if ($null -ne $script:RmLocCounts) { return }
    $t = @{}; $z = @{}
    $p = Get-RmNppesLocIndexPath
    if (Test-Path -LiteralPath $p) {
        foreach ($line in [System.IO.File]::ReadLines($p)) {
            $f = $line.Split('|')
            if ($f.Count -lt 2 -or -not $f[0]) { continue }
            $n = 0
            if ([int]::TryParse($f[1], [ref]$n)) { $t[$f[0]] = $n }
            if ($f.Count -ge 3 -and $f[2]) { $z[$f[0]] = $f[2] }
        }
    }
    $script:RmLocCounts = $t
    $script:RmLocZips = $z
}

function Get-RmSecondaryLocationCount([string]$Npi) {
    Initialize-RmLocTables
    if ($script:RmLocCounts.ContainsKey($Npi)) { $script:RmLocCounts[$Npi] } else { 0 }
}

function Get-RmSecondaryLocationZipTable {
    Initialize-RmLocTables
    $script:RmLocZips
}

$script:RmOtherNames = $null
function Get-RmOtherNameTable {
    # NPI -> List of "doing business as" names (NPPES other-name file).
    # 31.8% of therapy organizations trade under a DBA whose normalized form
    # differs from the legal name (measured) - 'ADVANCED PHYSICAL THERAPY,
    # LLC' IS an 'ATI Physical Therapy' clinic. Loaded once per process;
    # absent file = empty table and every consumer degrades to legal names.
    if ($null -ne $script:RmOtherNames) { return $script:RmOtherNames }
    $t = @{}
    $p = Get-RmNppesOtherNamePath
    if (Test-Path -LiteralPath $p) {
        foreach ($line in [System.IO.File]::ReadLines($p)) {
            $i = $line.IndexOf('|')
            if ($i -lt 1) { continue }
            $n = $line.Substring(0, $i)
            if (-not $t.ContainsKey($n)) { $t[$n] = New-Object System.Collections.Generic.List[string] }
            $t[$n].Add($line.Substring($i + 1))
        }
    }
    $script:RmOtherNames = $t
    $t
}

$script:RmDacRoster = $null
function Get-RmCareCompareRosterSize([string]$OrgName) {
    # Distinct clinicians on Care Compare practicing under this organization
    # NAME (all locations combined - same name-key the by-address view uses).
    # 0 means "not listed on Care Compare", not "no staff": cash-pay and
    # recently-enrolled clinics sit outside that roster.
    if (-not $OrgName) { return 0 }
    if ($null -eq $script:RmDacRoster) {
        $p = Get-RmDacIndexPath
        $script:RmDacRoster = if (Test-Path -LiteralPath $p) {
            [RmEngine]::ScanDacRosterCounts($p, 0, 2)
        } else { New-Object 'System.Collections.Generic.Dictionary[string,int]' }
    }
    $k = Get-RmOrgNameKey $OrgName
    if ($k -and $script:RmDacRoster.ContainsKey($k)) { $script:RmDacRoster[$k] } else { 0 }
}

function Get-RmClosestSiteDistance {
    # Distance from a reference point to a provider's CLOSEST known location:
    # the row's own ZIP plus every registered secondary-location ZIP. Without
    # this, a chain registered 20 miles out whose clinic sits 2 miles away
    # reads as "20 miles" in competitor tables. Returns $null when nothing
    # is locatable.
    param([string]$Npi, [string]$RowZip, $RefLoc, $Cents)
    if ($null -eq $RefLoc) { return $null }
    $zips = New-Object System.Collections.Generic.List[string]
    if ($RowZip) { $zips.Add($RowZip) }
    $tbl = Get-RmSecondaryLocationZipTable
    if ($tbl.ContainsKey($Npi)) { foreach ($z in ([string]$tbl[$Npi]).Split(';')) { $zips.Add($z) } }
    $best = $null
    foreach ($z in $zips) {
        if (-not $z -or $z -notmatch '^\d{5}$' -or -not $Cents.ContainsKey($z)) { continue }
        $d = Get-RmMilesBetween $RefLoc[0] $RefLoc[1] $Cents[$z][0] $Cents[$z][1]
        if ($null -eq $best -or $d -lt $best) { $best = $d }
    }
    $best
}

function Test-RmTherapyPracticeName([string]$Name) {
    # STRICT: the name must literally say it is a PT/OT/SLP practice, and
    # must not be a facility type that merely mentions therapy. Used to
    # rescue orgs that registered NO therapy taxonomy (measured: 469 such
    # orgs carried 785,074 patients in 2022; most hold the legacy
    # 'Specialist' code 174400000X).
    if (-not $Name) { return $false }
    $u = $Name.ToUpperInvariant()
    if ($u -notmatch 'PHYSICAL THERAP|OCCUPATIONAL THERAP|SPEECH THERAP|SPEECH.LANGUAGE PATHOLOG') { return $false }
    if ($u -match 'HOSPITAL|MEDICAL CENTER|HEALTH SYSTEM|NURSING|HOME HEALTH|HOME CARE|HOSPICE|SCHOOL|UNIVERSITY|COLLEGE|STAFFING|REGISTRY|INSURANCE|EQUIPMENT|SUPPLY|ACADEM') { return $false }
    $true
}

function Get-RmIndexPrimaryCode([string[]]$f) {
    # Index layout: code_1 at 8, codes 2-15 at 10..23, Switch_1..15 at 24..38.
    # Returns the code whose switch is 'Y'; falls back to slot 1 when the
    # index predates switch fields or no slot is flagged.
    for ($k = 1; $k -le 15; $k++) {
        $si = 23 + $k
        if ($si -ge $f.Count) { break }
        if ($f[$si] -eq 'Y') {
            $ci = if ($k -eq 1) { 8 } else { 8 + $k }
            if ($ci -lt $f.Count -and $f[$ci]) { return $f[$ci] }
        }
    }
    if ($f.Count -gt 8) { $f[8] } else { '' }
}
function Get-RmDacIndexPath { Join-Path $script:RmConfig.DataDir 'care-compare-index.psv' }

$script:RmNuccNames = $null
function Get-RmTaxonomyName([string]$Code) {
    if ($null -eq $script:RmNuccNames) {
        $t = @{}
        $p = Join-Path $PSScriptRoot 'nucc-taxonomy.csv'
        if (Test-Path -LiteralPath $p) {
            foreach ($line in [System.IO.File]::ReadLines($p)) {
                $i = $line.IndexOf(',')
                if ($i -gt 0) { $t[$line.Substring(0, $i)] = $line.Substring($i + 1) }
            }
        }
        $script:RmNuccNames = $t
    }
    if ($Code -and $script:RmNuccNames.ContainsKey($Code)) { $script:RmNuccNames[$Code] } else { $Code }
}

function Import-RmNppesBulk {
    <#
    .SYNOPSIS
      Builds the local NPPES lookup index from the monthly NPPES Data
      Dissemination file (the ~1 GB zip from download.cms.gov/nppes, or the
      extracted npidata_pfile CSV). One streaming pass; afterwards provider
      lookups run offline against the index instead of the live registry.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    Initialize-RmDataDir | Out-Null
    # Fields 0-9 are fixed; SECONDARY taxonomies are appended (10+) so field
    # positions stay stable. All 15 taxonomy slots are indexed because a
    # provider can hold therapy as a secondary taxonomy — indexing only the
    # primary made a live 10-mile sweep miss 43 of 1,778 real providers.
    $cols = @('NPI', 'Entity Type Code', 'Provider Organization Name (Legal Business Name)',
        'Provider Last Name (Legal Name)', 'Provider First Name',
        'Provider Business Practice Location Address City Name',
        'Provider Business Practice Location Address State Name',
        'Provider Business Practice Location Address Postal Code',
        'Healthcare Provider Taxonomy Code_1', 'Provider Enumeration Date') +
        @(2..15 | ForEach-Object { "Healthcare Provider Taxonomy Code_$_" }) +
        @(1..15 | ForEach-Object { "Healthcare Provider Primary Taxonomy Switch_$_" })
        # Switch_N = 'Y' marks which slot is the PRIMARY taxonomy. Slot ORDER
        # does not: Banner Boswell hospital carries a rehab-clinic code in
        # slot 1 with 'Y' on its hospital code - treating slot 1 as primary
        # put a 319,024-patient hospital atop a therapy ranking.
    $src = $Path; $tmpExtract = $null
    try {
        $tmpOut = (Get-RmNppesIndexPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        if ([System.IO.Path]::GetExtension($Path).ToLowerInvariant() -eq '.zip') {
            # Stream the 11+ GB inner CSV straight out of the zip - it is
            # never extracted (it would not fit on a small disk).
            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
            $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
            try {
                $entry = @($zip.Entries | Where-Object { $_.Name -match '^npidata_pfile_[\d-]+\.csv$' } |
                    Sort-Object Length -Descending)
                if (-not $entry.Count) { throw "No npidata_pfile CSV found inside '$Path' - is this the NPPES Data Dissemination zip?" }
                $stream = $entry[0].Open()
                try { $rows = [RmEngine]::BuildRosterIndexFromStream($stream, $tmpOut, $cols) }
                finally { $stream.Dispose() }
            } finally { $zip.Dispose() }
        } else {
            $rows = [RmEngine]::BuildRosterIndex($src, $tmpOut, $cols)
        }
        Move-Item -LiteralPath $tmpOut -Destination (Get-RmNppesIndexPath) -Force
        # The chain table is DERIVED from this index, so a rebuild must drop
        # it or the next flag would come from the previous file. Build it now
        # (~25s on the real file) rather than making the user wait inside
        # their first ZIP search; a failure here must not fail the import,
        # since Get-RmChainIndex rebuilds on demand anyway.
        $script:RmChainIdx = $null
        try {
            $ctmp = (Get-RmChainIndexPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
            [void][RmEngine]::BuildChainIndex((Get-RmNppesIndexPath), $ctmp)
            Move-Item -LiteralPath $ctmp -Destination (Get-RmChainIndexPath) -Force
        } catch {
            Write-Warning "Chain table not pre-built ($($_.Exception.Message)); it will be built on first use."
        }
        # Secondary practice locations (pl_pfile) -> NPI|count|zip1;zip2;...
        # The counts confirm multi-site NPIs; the ZIPs let area sweeps list a
        # practice that TREATS in the searched area but is REGISTERED outside
        # it (measured in St Louis: AXES Physical Therapy, 19,287 patients
        # and five in-ring clinics, registered one town past the radius).
        # Sparse in practice (many chains register none), so it CONFIRMS
        # multi-site but its absence proves nothing.
        $locRows = 0
        try {
            if ([System.IO.Path]::GetExtension($Path).ToLowerInvariant() -eq '.zip') {
                $zip2 = [System.IO.Compression.ZipFile]::OpenRead($Path)
                try {
                    $ple = @($zip2.Entries | Where-Object { $_.Name -match '^pl_pfile_[\d-]+\.csv$' } |
                        Sort-Object Length -Descending)
                    if ($ple.Count) {
                        $st2 = $ple[0].Open()
                        try {
                            $locTmp = (Get-RmNppesLocIndexPath) + '.raw'
                            [void][RmEngine]::BuildRosterIndexFromStream($st2, $locTmp,
                                @('NPI', 'Provider Secondary Practice Location Address - Postal Code'))
                            $counts = @{}
                            $locZips = @{}
                            foreach ($ln in [System.IO.File]::ReadLines($locTmp)) {
                                $i = $ln.IndexOf('|')
                                if ($i -gt 0) {
                                    $k = $ln.Substring(0, $i)
                                    if ($counts.ContainsKey($k)) { $counts[$k]++ } else { $counts[$k] = 1 }
                                    $pz = $ln.Substring($i + 1)
                                    if ($pz.Length -ge 5) {
                                        $z5 = $pz.Substring(0, 5)
                                        if ($z5 -match '^\d{5}$') {
                                            if (-not $locZips.ContainsKey($k)) { $locZips[$k] = New-Object 'System.Collections.Generic.HashSet[string]' }
                                            [void]$locZips[$k].Add($z5)
                                        }
                                    }
                                }
                            }
                            $locOutTmp = (Get-RmNppesLocIndexPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
                            $sw2 = New-Object System.IO.StreamWriter($locOutTmp, $false, (New-Object System.Text.UTF8Encoding($false)))
                            try {
                                foreach ($kv in $counts.GetEnumerator()) {
                                    $zl = if ($locZips.ContainsKey($kv.Key)) { @($locZips[$kv.Key]) -join ';' } else { '' }
                                    $sw2.WriteLine($kv.Key + '|' + $kv.Value + '|' + $zl); $locRows++
                                }
                            }
                            finally { $sw2.Dispose() }
                            Move-Item -LiteralPath $locOutTmp -Destination (Get-RmNppesLocIndexPath) -Force
                            Remove-Item -LiteralPath $locTmp -Force -ErrorAction SilentlyContinue
                        } finally { $st2.Dispose() }
                    }
                } finally { $zip2.Dispose() }
            }
        } catch { Write-Warning "Secondary practice locations could not be indexed (multi-site detection falls back to scale): $($_.Exception.Message)" }
        # "Doing business as" names (othername_pfile) -> NPI|OtherName rows.
        # A third of therapy orgs trade under a DBA that differs from the
        # legal name; name searches match BOTH once this index exists.
        $dbaRows = 0
        try {
            if ([System.IO.Path]::GetExtension($Path).ToLowerInvariant() -eq '.zip') {
                $zip3 = [System.IO.Compression.ZipFile]::OpenRead($Path)
                try {
                    $one = @($zip3.Entries | Where-Object { $_.Name -match '^othername_pfile_[\d-]+\.csv$' } |
                        Sort-Object Length -Descending)
                    if ($one.Count) {
                        $st3 = $one[0].Open()
                        try {
                            $onTmp = (Get-RmNppesOtherNamePath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
                            $dbaRows = [RmEngine]::BuildRosterIndexFromStream($st3, $onTmp,
                                @('NPI', 'Provider Other Organization Name'))
                            Move-Item -LiteralPath $onTmp -Destination (Get-RmNppesOtherNamePath) -Force
                        } finally { $st3.Dispose() }
                    }
                } finally { $zip3.Dispose() }
            }
        } catch { Write-Warning "Other-name (DBA) file could not be indexed (name searches match legal names only): $($_.Exception.Message)" }
        [pscustomobject]@{ Rows = $rows; SecondaryLocationRows = $locRows; Path = Get-RmNppesIndexPath
            Message = "NPPES bulk index built: $('{0:N0}' -f $rows) providers$(if ($locRows -gt 0) { "; $('{0:N0}' -f $locRows) secondary-location records (area searches now also find practices registered elsewhere that treat locally)" })$(if ($dbaRows -gt 0) { "; $('{0:N0}' -f $dbaRows) doing-business-as names (name searches match brands AND legal names)" }). Lookups now run locally (the live registry stays as fallback)." }
    } finally {
        if ($tmpExtract -and (Test-Path -LiteralPath $tmpExtract)) { Remove-Item -LiteralPath $tmpExtract -Force }
    }
}

function Import-RmCareCompare {
    <#
    .SYNOPSIS
      Builds the practice-group membership index from CMS's Care Compare
      "Doctors and Clinicians National Downloadable File" (DAC CSV). Used to
      suggest which NPIs belong to the same group for a combined analysis.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    Initialize-RmDataDir | Out-Null
    $cols = @('NPI', 'org_pac_id', 'Facility Name', 'pri_spec',
        'Provider Last Name', 'Provider First Name', 'City/Town', 'State',
        'adr_ln_1', 'ZIP Code')   # appended: existing field indexes stay valid
    $tmpOut = (Get-RmDacIndexPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $rows = [RmEngine]::BuildRosterIndex($Path, $tmpOut, $cols)
    Move-Item -LiteralPath $tmpOut -Destination (Get-RmDacIndexPath) -Force
    [pscustomobject]@{ Rows = $rows; Path = Get-RmDacIndexPath
        Message = "Care Compare index built: $('{0:N0}' -f $rows) clinician rows. The Source analysis can now suggest affiliated NPIs to combine." }
}

function Get-RmAffiliatedNpi {
    <#
    .SYNOPSIS
      Therapy clinicians sharing a practice group (Care Compare org_pac_id)
      with the given NPI - the NPIs worth combining in one analysis.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidatePattern('^\d{10}$')][string]$Npi)
    $idx = Get-RmDacIndexPath
    if (-not (Test-Path -LiteralPath $idx)) { return @() }
    $mine = New-Object 'System.Collections.Generic.HashSet[string]'
    [void]$mine.Add($Npi)
    $pacs = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($line in @([RmEngine]::ScanRosterIndex($idx, $mine, 0))) {
        $f = $line.Split('|')
        if ($f.Count -ge 2 -and $f[1]) { [void]$pacs.Add($f[1]) }
    }
    if (-not $pacs.Count) { return @() }
    $out = @{}
    foreach ($line in @([RmEngine]::ScanRosterIndex($idx, $pacs, 1))) {
        $f = $line.Split('|')
        if ($f.Count -lt 8 -or $f[0] -eq $Npi -or $out.ContainsKey($f[0])) { continue }
        if ($f[3] -notmatch '(?i)physical therap|occupational therap|speech') { continue }
        $out[$f[0]] = [pscustomobject]@{
            NPI = $f[0]; Name = ("$($f[5]) $($f[4])").Trim(); Specialty = $f[3]
            Group = $f[2]; City = $f[6]; State = $f[7]
        }
    }
    @($out.Values | Sort-Object Name)
}

function Get-RmEnrollmentIndexPath { Join-Path $script:RmConfig.DataDir 'enrollment-index.psv' }

function Import-RmEnrollment {
    <#
    .SYNOPSIS
      Builds the local county-market index from CMS's Medicare Monthly
      Enrollment CSV, so market context (beneficiaries, Medicare Advantage
      share) works with NO internet connection.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)
    Initialize-RmDataDir | Out-Null
    $cols = @('BENE_FIPS_CD', 'YEAR', 'MONTH', 'BENE_COUNTY_DESC', 'BENE_STATE_ABRVTN',
        'TOT_BENES', 'ORGNL_MDCR_BENES', 'MA_AND_OTH_BENES')
    $tmpOut = (Get-RmEnrollmentIndexPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $rows = [RmEngine]::BuildRosterIndex($Path, $tmpOut, $cols)
    Move-Item -LiteralPath $tmpOut -Destination (Get-RmEnrollmentIndexPath) -Force
    [pscustomobject]@{ Rows = $rows; Path = Get-RmEnrollmentIndexPath
        Message = "Enrollment index built: $('{0:N0}' -f $rows) county-month rows. County market context now works offline." }
}

# Every supporting resource the app can download, with the local file name
# it is stored under. Kept in ONE place so the downloader, the manifest and
# the docs cannot drift apart.
$script:RmResources = @(
    [ordered]@{ Key = 'nppes'; Name = 'NPPES bulk provider file'
        Url = 'https://download.cms.gov/nppes/NPI_Files.html'
        File = 'NPPES_Data_Dissemination.zip'; ApproxMB = 1100
        Note = 'Monthly full registry. Import with Import-RmNppesBulk (streams from the zip; do not unzip).'
        Direct = $false }
    [ordered]@{ Key = 'carecompare'; Name = 'Care Compare clinician file (DAC)'
        Url = 'https://data.cms.gov/provider-data/sites/default/files/resources/52c3f098d7e56028a298fd297cb0b38d_1782750575/DAC_NationalDownloadableFile.csv'
        File = 'DAC_NationalDownloadableFile.csv'; ApproxMB = 800
        Note = 'Clinician-to-group membership. Import with Import-RmCareCompare.'
        Direct = $true }
    [ordered]@{ Key = 'enrollment'; Name = 'Medicare Monthly Enrollment'
        Url = 'https://data.cms.gov/data-api/v1/dataset/d7fabe1e-d19b-4333-9eff-e80e0643f2fd/data?size=5000&offset=0'
        File = 'medicare-monthly-enrollment.csv'; ApproxMB = 60
        Note = 'County beneficiaries + Medicare Advantage share. Import with Import-RmEnrollment.'
        Direct = $true; Csv = $true }
)

# One picture of which supporting indexes exist locally, for the GUI's
# setup section. File sizes, not row counts - counting a 687 MB file takes
# seconds and this must be instant on tab load.
function Get-RmLocalDataStatus {
    $probe = {
        param($Path)
        if (Test-Path -LiteralPath $Path) {
            $fi = Get-Item -LiteralPath $Path
            [pscustomobject]@{ Present = $true; SizeMB = [math]::Round($fi.Length / 1MB, 1)
                               Updated = $fi.LastWriteTime.ToString('yyyy-MM-dd') }
        } else {
            [pscustomobject]@{ Present = $false; SizeMB = 0; Updated = '' }
        }
    }
    [pscustomobject]@{
        Nppes       = & $probe (Get-RmNppesIndexPath)      # discovery, chain flags, market capture
        CareCompare = & $probe (Get-RmDacIndexPath)        # by-address breakdown, affiliated NPIs
        Enrollment  = & $probe (Get-RmEnrollmentIndexPath) # county market with no internet
        ChainTable  = & $probe (Get-RmChainIndexPath)      # derived from the NPPES index
    }
}

# The NPPES Data Dissemination zip cannot be auto-downloaded (no stable
# URL), but it CAN be auto-found once the user has it: the monthly file
# lands in predictable places with a predictable name. Scans drive roots
# (fixed and removable - users park 1.1 GB files on E:\), Downloads, and
# the app's data folder; never recursive, so it is instant. Newest wins,
# judged by the month/year/version IN THE NAME (July_2026_V2 beats
# July_2026 beats June_2026); an unparseable name falls back to its file
# date and never outranks a parseable one.
function Find-RmNppesDisseminationFile {
    [CmdletBinding()]
    param([string[]]$SearchPath)
    $dirs = New-Object System.Collections.Generic.List[string]
    if ($SearchPath) {
        foreach ($p in $SearchPath) { $dirs.Add($p) }
    } else {
        try {
            foreach ($dr in [System.IO.DriveInfo]::GetDrives()) {
                try {
                    if ($dr.IsReady -and ($dr.DriveType -eq 'Fixed' -or $dr.DriveType -eq 'Removable')) {
                        $dirs.Add($dr.RootDirectory.FullName)
                    }
                } catch { }
            }
        } catch { }
        foreach ($p in @((Join-Path $HOME 'Downloads'),
                         $script:RmConfig.DataDir,
                         (Join-Path $script:RmConfig.DataDir 'resources'))) { $dirs.Add($p) }
    }
    $months = @{ JANUARY = 1; FEBRUARY = 2; MARCH = 3; APRIL = 4; MAY = 5; JUNE = 6
                 JULY = 7; AUGUST = 8; SEPTEMBER = 9; OCTOBER = 10; NOVEMBER = 11; DECEMBER = 12 }
    $best = $null; $bestTier = -1; $bestKey = [long]-1
    foreach ($dir in $dirs) {
        if (-not $dir) { continue }
        $files = @()
        try {
            if (Test-Path -LiteralPath $dir) {
                $files = @(Get-ChildItem -LiteralPath $dir -Filter 'NPPES_Data_Dissemination*.zip' -File -ErrorAction SilentlyContinue)
            }
        } catch { continue }
        foreach ($f in $files) {
            $tier = 0; $key = [long]$f.LastWriteTimeUtc.Ticks; $label = ''
            if ($f.Name -match '(?i)NPPES_Data_Dissemination_([A-Za-z]+)_(\d{4})(?:_V(\d+))?') {
                $mn = $Matches[1].ToUpperInvariant()
                if ($months.ContainsKey($mn)) {
                    $tier = 1
                    $v = if ($Matches[3]) { [int]$Matches[3] } else { 1 }
                    $key = [long]([int]$Matches[2] * 10000 + $months[$mn] * 100 + $v)
                    $label = ('{0} {1}{2}' -f ($Matches[1].Substring(0,1).ToUpperInvariant() + $Matches[1].Substring(1).ToLowerInvariant()),
                              $Matches[2], $(if ($v -gt 1) { " V$v" } else { '' }))
                }
            }
            if ($tier -gt $bestTier -or ($tier -eq $bestTier -and $key -gt $bestKey)) {
                $bestTier = $tier; $bestKey = $key
                $best = [pscustomobject]@{
                    Path = $f.FullName
                    SizeMB = [math]::Round($f.Length / 1MB, 1)
                    Label = if ($label) { $label } else { $f.LastWriteTime.ToString('yyyy-MM-dd') }
                    LastWrite = $f.LastWriteTimeUtc
                }
            }
        }
    }
    $best
}

function Save-RmLocalResources {
    <#
    .SYNOPSIS
      Downloads every supporting data file to a LOCAL folder and writes a
      manifest (size, SHA-256, date, source URL) so the app never depends on
      a URL staying alive. Files already present are skipped unless -Force.
    .NOTES
      The NPPES bulk file has no stable direct URL (the file name carries a
      date), so it is reported as a manual step with its page link rather
      than guessed at.
    #>
    [CmdletBinding()]
    param(
        [string]$Destination,
        [switch]$Force
    )
    $dest = if ($Destination) { $Destination } else { Join-Path $script:RmConfig.DataDir 'resources' }
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    $manifestPath = Join-Path $dest 'MANIFEST.txt'
    $lines = New-Object System.Collections.Generic.List[object]
    $lines.Add("Medicare Order & Referring Tracker - local resource manifest")
    $lines.Add("Written $(Get-Date -Format 'yyyy-MM-dd HH:mm')")
    $lines.Add('')
    $results = New-Object System.Collections.Generic.List[object]
    foreach ($res in $script:RmResources) {
        $target = Join-Path $dest $res.File
        $status = ''
        if (-not $res.Direct) {
            $status = 'MANUAL - no stable direct link'
        } elseif ((Test-Path -LiteralPath $target) -and -not $Force) {
            $status = 'already present'
        } else {
            $tmp = '{0}.{1}.part' -f $target, [guid]::NewGuid().ToString('N')
            try {
                Write-Verbose "Downloading $($res.Name) (~$($res.ApproxMB) MB)..."
                # One retry: a Wi-Fi blip 700 MB into an 800 MB file should
                # not cost the user the whole download session.
                try {
                    Invoke-WebRequest -Uri $res.Url -OutFile $tmp -TimeoutSec 1800 -UseBasicParsing -ErrorAction Stop
                } catch {
                    Write-Warning "$($res.Name): download interrupted ($($_.Exception.Message)); retrying once..."
                    Start-Sleep -Seconds 3
                    Invoke-WebRequest -Uri $res.Url -OutFile $tmp -TimeoutSec 1800 -UseBasicParsing -ErrorAction Stop
                }
                Move-Item -LiteralPath $tmp -Destination $target -Force
                $status = 'downloaded'
            } catch {
                $status = "FAILED: $($_.Exception.Message)"
                Write-Warning "$($res.Name): $status"
            } finally {
                # An abandoned partial is NOT swept by the *.tmp cleanup, so
                # never leave one - it would sit there at up to 800 MB forever.
                if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
            }
        }
        $size = if (Test-Path -LiteralPath $target) { (Get-Item -LiteralPath $target).Length } else { 0 }
        $sha = if ($size -gt 0) { (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash } else { '' }
        $results.Add([pscustomobject]@{
            Key = $res.Key; Name = $res.Name; File = $res.File; Path = $target
            Status = $status; Bytes = $size; Sha256 = $sha; Url = $res.Url })
        $lines.Add("$($res.Name)")
        $lines.Add("  file:   $($res.File)")
        $lines.Add("  status: $status")
        $lines.Add("  bytes:  $('{0:N0}' -f $size)")
        $lines.Add("  sha256: $sha")
        $lines.Add("  source: $($res.Url)")
        $lines.Add("  use:    $($res.Note)")
        $lines.Add('')
    }
    $lines.Add('Bundled with the app (no download needed): ZIP centroids (US Census ZCTA gazetteer),')
    $lines.Add('ZIP-to-county crosswalk, NUCC taxonomy names, and the offline map library.')
    $lines | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    [pscustomobject]@{ Destination = $dest; Manifest = $manifestPath; Files = @($results.ToArray()) }
}

# Corporate-form words that carry no identity. Measured over the 1,949,379
# organization names in the real NPPES file, where the most common final
# words are LLC 28.7%, INC 20.8%, PLLC 6.0%, PC 4.7%, PA 2.5%, LTD 0.7%.
$script:RmOrgNoiseWords = 'LLC|INC|INCORPORATED|PC|PA|PLLC|LLP|LLLP|LP|LTD|LIMITED|PLC|SC|PSC|APC|CORP|CORPORATION|COMPANY|CO|PARTNERSHIP|THE|OF|AND'

function Get-RmOrgNameKey([string]$Name) {
    # Collapses the corporate-suffix noise that makes one chain look like
    # many companies: "IVYREHAB NETWORK, INC." / "INC." / "INC" -> one key.
    $n = $Name.ToUpperInvariant()
    $n = [regex]::Replace($n, '[^A-Z0-9 ]', ' ')
    # Collapse runs of spaces FIRST: the single-letter merge below matches on
    # one space, and the C# twin splits on whitespace regardless of width, so
    # "A  B" would key differently in each. (The drift test caught exactly
    # this.)
    $n = [regex]::Replace($n, '\s+', ' ').Trim()
    # Punctuated forms tokenize apart: "P.C." becomes "P C", so 52,156 real
    # names end in a bare "C" and 20,777 in "A" and would never match their
    # unpunctuated twins. Glue runs of single letters back together.
    $n = [regex]::Replace($n, '\b[A-Z](?: [A-Z]\b)+', { $args[0].Value -replace ' ', '' })
    $n = [regex]::Replace($n, "\b($script:RmOrgNoiseWords)\b", ' ')
    [regex]::Replace($n, '\s+', ' ').Trim()
}

# Matching a user-typed organization name against stored name keys.
# Brands are inconsistent about spacing - NPPES holds 60 'IVYREHAB ...'
# entities and one 'VIRTUA IVY REHAB', and the brand itself writes
# 'Ivy Rehab' - so every space the USER types is treated as optional:
# 'IVY REHAB' matches both 'IVY REHAB ...' and 'IVYREHAB ...'. The reverse
# (ignoring spaces inside the STORED name) is deliberately NOT matched -
# compacting 'ATHLETIC ORTHOPEDIC' makes it contain 'ATHLETICO', which is
# how an unrelated knee clinic ends up inside a chain - but such
# compact-only near-misses are COUNTED so callers can surface them as
# suggestions instead of silently dropping them.
function Get-RmNameMatcher([string]$Needle) {
    $rx = [regex]::Escape($Needle) -replace '\\ ', ' ?'
    [pscustomobject]@{
        Regex = [regex]::new($rx)
        Compact = ($Needle -replace ' ', '')
    }
}

function Get-RmProviderFamily {
    <#
    .SYNOPSIS
      Breaks a multi-location provider organization down by NPI: every NPI
      whose name matches, its REGISTERED address, its measured referral
      volume and distinct source count, and whether that NPI covers more
      than one site. Rolls the whole family up so chain totals are visible.
    .NOTES
      HARD LIMIT: shared-patient data records NPI pairs and carries NO
      service address, so where one NPI bills for several clinics its
      volume CANNOT be split per location. What is reliable is per-NPI
      volume and the address that NPI is registered at.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateLength(3, 100)][string]$Name,
        [ValidatePattern('^[A-Za-z]{2}$')][string]$State
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $needle = Get-RmOrgNameKey $Name
    if (-not $needle) { throw "Enter part of an organization name (letters or numbers)." }

    $members = @{}
    $m0 = Get-RmNameMatcher $needle
    $nearMiss = New-Object 'System.Collections.Generic.HashSet[string]'
    $idx = Get-RmNppesIndexPath
    if (Test-Path -LiteralPath $idx) {
        # Name matching runs in C# (key normalisation + the space-optional
        # regex per 9.7M rows was this search's dominant cost); spacing
        # near-misses accumulate into $nearMiss on the way through.
        foreach ($line in @([RmEngine]::FindByOrgName($idx, 1, 2, $m0.Regex.ToString(), $m0.Compact, $nearMiss))) {
            $f = $line.Split('|')
            if ($f.Count -lt 10) { continue }
            if ($State -and $f[6] -ne $State.ToUpperInvariant()) { continue }
            $members[$f[0]] = [pscustomobject]@{
                NPI = $f[0]; Name = $f[2]; FamilyKey = (Get-RmOrgNameKey $f[2])
                City = $f[5]; State = $f[6]
                Zip = $(if ($f[7].Length -ge 5) { $f[7].Substring(0, 5) } else { $f[7] })
                MatchedVia = ''
            }
        }
        # DBA pass: clinics often enroll under a holding/legal name while
        # trading as the brand everyone searches for - 'ADVANCED PHYSICAL
        # THERAPY, LLC' IS an ATI clinic, and a legal-name search misses it.
        # The other-name index maps brand -> NPI; matched members join the
        # family flagged with the DBA that matched, so nothing is silent.
        $onPath = Get-RmNppesOtherNamePath
        if (Test-Path -LiteralPath $onPath) {
            $dbaHits = @{}
            foreach ($line in @([RmEngine]::FindByOrgName($onPath, -1, 1, $m0.Regex.ToString(), $m0.Compact, $nearMiss))) {
                $i = $line.IndexOf('|')
                if ($i -lt 1) { continue }
                $n0 = $line.Substring(0, $i)
                if (-not $members.ContainsKey($n0) -and -not $dbaHits.ContainsKey($n0)) {
                    $dbaHits[$n0] = $line.Substring($i + 1)
                }
            }
            if ($dbaHits.Count -gt 0) {
                $dbaWant = New-Object 'System.Collections.Generic.HashSet[string]'
                foreach ($k in $dbaHits.Keys) { [void]$dbaWant.Add($k) }
                foreach ($line in @([RmEngine]::ScanRosterIndex($idx, $dbaWant, 0))) {
                    $f = $line.Split('|')
                    if ($f.Count -lt 10 -or $f[1] -ne '2') { continue }
                    if ($State -and $f[6] -ne $State.ToUpperInvariant()) { continue }
                    $members[$f[0]] = [pscustomobject]@{
                        NPI = $f[0]; Name = $f[2]; FamilyKey = (Get-RmOrgNameKey $f[2])
                        City = $f[5]; State = $f[6]
                        Zip = $(if ($f[7].Length -ge 5) { $f[7].Substring(0, 5) } else { $f[7] })
                        MatchedVia = "DBA: $($dbaHits[$f[0]])"
                    }
                }
            }
        }
    } else {
        foreach ($r in @(Find-RmPractice -Name $Name -State:$State | Where-Object { $_.Type -eq 'Organization' })) {
            $members[$r.NPI] = [pscustomobject]@{
                NPI = $r.NPI; Name = $r.Name; FamilyKey = Get-RmOrgNameKey $r.Name
                City = $r.City; State = $r.State; Zip = $r.Zip
                MatchedVia = ''
            }
        }
    }
    if (-not $members.Count) { throw "No organization NPIs match '$Name'$(if ($State) { " in $State" })." }

    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($k in $members.Keys) { [void]$set.Add($k) }
    Write-Verbose "Scanning $($info.Label) for $($set.Count) NPI(s) in this organization..."
    $agg = @{}
    foreach ($e in @([RmEngine]::ScanInbound($info.Path, $set, $info.Format))) {
        if (-not $agg.ContainsKey($e.TargetNpi)) { $agg[$e.TargetNpi] = [pscustomobject]@{ Benes = 0; Sources = 0 } }
        $agg[$e.TargetNpi].Benes += $e.BeneCount
        $agg[$e.TargetNpi].Sources += 1
    }

    $rows = foreach ($m in $members.Values) {
        $a = if ($agg.ContainsKey($m.NPI)) { $agg[$m.NPI] } else { $null }
        $sec = Get-RmSecondaryLocationCount $m.NPI
        $srcN = if ($a) { $a.Sources } else { 0 }
        [pscustomobject]@{
            NPI = $m.NPI; Name = $m.Name
            City = $m.City; State = $m.State; Zip = $m.Zip
            ReferralSources = $srcN
            SharedPatients = $(if ($a) { $a.Benes } else { 0 })
            PracticeSites = $(if ($sec -gt 0) { $sec + 1 } else { 1 })
            MultiSiteNPI = $(if ($sec -gt 0) { 'Yes (registry)' }
                             elseif ($srcN -ge $script:RmSingleSiteSourceCeiling) { 'Likely (scale)' }
                             else { '' })
            Chain = Get-RmChainMark $m.Name
            MatchedVia = $(if ($null -ne $m.PSObject.Properties['MatchedVia']) { [string]$m.MatchedVia } else { '' })
            FamilyKey = $m.FamilyKey
        }
    }
    $rows = @($rows | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                            @{Expression = 'NPI'; Descending = $false})
    $totVol = 0; $totSrc = 0; $withVol = 0; $multi = 0
    foreach ($r in $rows) {
        $totVol += [int]$r.SharedPatients; $totSrc += [int]$r.ReferralSources
        if ([int]$r.SharedPatients -gt 0) { $withVol++ }
        if ($r.MultiSiteNPI) { $multi++ }
    }
    $byState = @($rows | Group-Object State | ForEach-Object {
        $v = 0; foreach ($x in $_.Group) { $v += [int]$x.SharedPatients }
        [pscustomobject]@{ State = $_.Name; Npis = $_.Count; SharedPatients = $v
            PctOfFamily = if ($totVol -gt 0) { [math]::Round(100.0 * $v / $totVol, 1) } else { 0 } }
    } | Sort-Object SharedPatients -Descending)

    $notes = @(Get-RmMethodologyNotes -Info $info) + @(
        ''
        "ORGANIZATION BREAKDOWN: every NPPES ORGANIZATION NPI whose name matches '$Name'$(if ($State) { " in $State" }), with its registered practice address and its own measured referral volume on $($info.Label)."
        'PER-LOCATION LIMIT: the shared-patient file records NPI-to-NPI pairs and carries NO service address. Where one NPI bills for several clinics, its volume covers ALL of them and CANNOT be split per site. Rows flagged in MultiSiteNPI are exactly those cases.'
        "MultiSiteNPI = 'Yes (registry)' when NPPES lists extra practice locations for that NPI; 'Likely (scale)' when it draws $($script:RmSingleSiteSourceCeiling)+ distinct referring providers, which no single outpatient site plausibly does (a large one-location practice measured 379)."
        'Chains also enumerate REGIONAL entities under slightly different names (e.g. "<Brand> New Hampshire, LLC"); those appear as separate rows here when the name still matches, and under their own FamilyKey.'
        'A blank volume means that NPI has no measured pair in this data year - commonly a location that bills through a sibling NPI, or one whose pairs all fell under the 11-patient floor.'
        $(if ($nearMiss.Count) {
            "SPACING NEAR-MISS: $($nearMiss.Count) organization(s) match only when spaces are ignored and were NOT included: " +
            ((@($nearMiss) | Select-Object -First 5) -join '; ') +
            $(if ($nearMiss.Count -gt 5) { '; ...' }) +
            '. Search that exact name to include it - ignoring spaces automatically would also merge unrelated names (compacting ATHLETIC ORTHOPEDIC creates ATHLETICO).'
        })
        $(if (@($rows | Where-Object { $_.Chain }).Count) { Get-RmChainNote })
    ) | Where-Object { $_ }
    [pscustomobject]@{
        Search = $Name
        Year = $info.Year
        Label = $info.Label
        Npis = @($rows).Count
        NpisWithVolume = $withVol
        MultiSiteNpis = $multi
        ChainNpis = @($rows | Where-Object { $_.Chain }).Count
        TotalPatients = $totVol
        TotalSourceLinks = $totSrc
        Rows = @($rows)
        ByState = @($byState)
        Notes = @($notes)
    }
}

function Get-RmRelatedOrgNames {
    <#
    .SYNOPSIS
      Other Care Compare facility names that plausibly belong to the same
      company as $Name, ranked by how many street addresses they hold.
    .NOTES
      Brands rarely enrol under the brand: ATI Physical Therapy's clinics are
      registered as 'ATI HOLDINGS, LLC' (137 addresses) plus state entities,
      so a search for 'ATI PHYSICAL THERAPY' finds only a 20-site joint
      venture. Matching on the LEADING WORD of the search recovers those.
      Suggestions only - nothing is merged automatically, because two
      companies can share a first word.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateLength(3, 100)][string]$Name,
        [int]$Top = 5
    )
    $idx = Get-RmDacIndexPath
    if (-not (Test-Path -LiteralPath $idx)) { return @() }
    $needle = Get-RmOrgNameKey $Name
    # The leading word, which is where a brand lives ('ATI PHYSICAL THERAPY'
    # -> 'ATI'). Under 3 characters it matches half the file, so we stop.
    $lead = Get-RmOrgNameKey (@($Name -split '\s+' | Where-Object { $_ })[0])
    if (-not $lead -or $lead.Length -lt 3) { return @() }

    # The grouping runs in C# - this was the last script loop over a
    # million-row index, and it crawled (measured ~40 min on the real DAC
    # file); the engine does it in seconds with identical semantics.
    $out = foreach ($row in @([RmEngine]::RelatedOrgAddressCounts($idx, 2, 8, 9, $lead, [string]$needle))) {
        $i = $row.LastIndexOf([char]1)
        if ($i -lt 1) { continue }
        [pscustomobject]@{ Name = $row.Substring(0, $i); Addresses = [int]$row.Substring($i + 1) }
    }
    @($out | Sort-Object -Property @{Expression = 'Addresses'; Descending = $true},
                                   @{Expression = 'Name'; Descending = $false} | Select-Object -First $Top)
}

function Get-RmLocationReferrals {
    <#
    .SYNOPSIS
      ADDRESS-level referral volume for a multi-location organization.
      Care Compare publishes which clinicians practice at each street
      address; those clinicians have their OWN NPIs, and the shared-patient
      file carries volume against them. Summing per address therefore
      yields per-location figures that an organization NPI cannot give.
    .NOTES
      Covers the share of care billed under INDIVIDUAL clinician NPIs.
      Volume billed under the organization's own NPI has no service address
      and stays unattributable - it is reported separately, never spread
      across sites. A clinician listed at several addresses is counted at
      each (the data cannot say which visit happened where); those rows are
      flagged and the affected volume is quantified.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateLength(3, 100)][string]$Name,
        [ValidatePattern('^[A-Za-z]{2}$')][string]$State,
        [ValidatePattern('^\d{5}$')][string]$Zip
    )
    $info = Get-RmDatasetInfo
    if (-not $info.Ready) {
        throw "No shared-patient dataset is available yet (Referral map tab: download the CMS dataset or import a CareSet file)."
    }
    $idx = Get-RmDacIndexPath
    if (-not (Test-Path -LiteralPath $idx)) {
        throw ("Address-level figures need the Care Compare clinician file. Click 'Download supporting data' on the " +
               "Multi-site chains tab (or run Import-RmCareCompare -Path <csv> against the National Downloadable File " +
               "from https://data.cms.gov/provider-data/dataset/mj5m-pzi6).")
    }
    $needle = Get-RmOrgNameKey $Name
    if (-not $needle) { throw "Enter part of an organization name (letters or numbers)." }

    $byAddr = @{}; $npiAddrs = @{}
    $m1 = Get-RmNameMatcher $needle
    $addrNearMiss = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($line in @([RmEngine]::FindByOrgName($idx, -1, 2, $m1.Regex.ToString(), $m1.Compact, $addrNearMiss))) {
        $f = $line.Split('|')
        if ($f.Count -lt 10) { continue }
        if ($State -and $f[7] -ne $State.ToUpperInvariant()) { continue }
        $z5 = if ($f[9].Length -ge 5) { $f[9].Substring(0, 5) } else { $f[9] }
        if ($Zip -and $z5 -ne $Zip) { continue }
        $key = "$($f[8])|$($f[6])|$($f[7])|$z5"
        if (-not $byAddr.ContainsKey($key)) {
            $byAddr[$key] = [pscustomobject]@{
                Address = $f[8]; City = $f[6]; State = $f[7]; Zip = $z5
                Facility = $f[2]
                Npis = (New-Object 'System.Collections.Generic.HashSet[string]')
            }
        }
        [void]$byAddr[$key].Npis.Add($f[0])
        if (-not $npiAddrs.ContainsKey($f[0])) { $npiAddrs[$f[0]] = (New-Object 'System.Collections.Generic.HashSet[string]') }
        [void]$npiAddrs[$f[0]].Add($key)
    }
    if (-not $byAddr.Count) {
        # Don't just say "nothing found" - a brand is usually enrolled under
        # a different legal name, so offer the candidates.
        $sugg = @(Get-RmRelatedOrgNames -Name $Name)
        $hint = if ($sugg.Count) {
            " Did you mean: " + (@($sugg | ForEach-Object { "$($_.Name) ($($_.Addresses) locations)" }) -join '; ') + "?"
        } else { ' Try a shorter fragment of the name.' }
        throw ("No Care Compare practice addresses match '$Name'$(if ($State) { " in $State" })$(if ($Zip) { " in $Zip" })." + $hint)
    }

    $set = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($k in $npiAddrs.Keys) { [void]$set.Add($k) }
    Write-Verbose "Scanning $($info.Label) for $($set.Count) clinician NPI(s) across $($byAddr.Count) address(es)..."
    $vol = @{}
    foreach ($e in @([RmEngine]::ScanInbound($info.Path, $set, $info.Format))) {
        if (-not $vol.ContainsKey($e.TargetNpi)) { $vol[$e.TargetNpi] = [pscustomobject]@{ Benes = 0; Sources = 0 } }
        $vol[$e.TargetNpi].Benes += $e.BeneCount
        $vol[$e.TargetNpi].Sources += 1
    }

    $rows = foreach ($kv in $byAddr.GetEnumerator()) {
        $b = $kv.Value
        $tv = 0; $ts = 0; $withV = 0; $shared = 0; $sharedVol = 0
        foreach ($n in $b.Npis) {
            $v = if ($vol.ContainsKey($n)) { $vol[$n] } else { $null }
            if ($v) { $tv += $v.Benes; $ts += $v.Sources; $withV++ }
            if ($npiAddrs[$n].Count -gt 1) { $shared++; if ($v) { $sharedVol += $v.Benes } }
        }
        # Lower bound: only the clinicians who work HERE AND NOWHERE ELSE.
        # SharedPatients is the upper bound (every shared clinician counted
        # in full). The site's true number lies between the two.
        $exclusive = $tv - $sharedVol
        [pscustomobject]@{
            Address = $b.Address; City = $b.City; State = $b.State; Zip = $b.Zip
            Facility = $b.Facility
            Clinicians = $b.Npis.Count
            CliniciansWithVolume = $withV
            SharedPatients = $tv              # upper bound
            ExclusivePatients = $exclusive    # lower bound: this site's own clinicians
            ReferralSources = $ts
            CliniciansAtOtherSites = $shared
            SharedSitePatients = $sharedVol   # of this row, also counted elsewhere
        }
    }
    $rows = @($rows | Sort-Object -Property @{Expression = 'SharedPatients'; Descending = $true},
                                            @{Expression = 'Address'; Descending = $false})
    $rowTotal = 0; $addrWithVol = 0
    foreach ($r in $rows) {
        $rowTotal += [int]$r.SharedPatients
        if ([int]$r.SharedPatients -gt 0) { $addrWithVol++ }
    }
    # The headline total must count each CLINICIAN once. Summing the address
    # rows would double-count anyone listed at several sites (measured: 350
    # IvyRehab addresses sum to 581,483, but the 1,668 clinicians behind them
    # hold 521,938 - an 11% overstatement if the row sum were the headline).
    $totVol = 0
    foreach ($n in $set) { if ($vol.ContainsKey($n)) { $totVol += [int]$vol[$n].Benes } }
    $double = $rowTotal - $totVol
    # The organization's OWN NPI volume, reported separately - it has no
    # service address and must never be spread across the sites.
    $orgVol = 0; $orgNpis = 0; $orgMeasured = $false
    # -State:'' would trip the pattern validator, so bind it only when set.
    $famArgs = @{ Name = $Name }
    if ($State) { $famArgs['State'] = $State }
    try {
        $fam = Get-RmProviderFamily @famArgs
        $orgVol = [int]$fam.TotalPatients; $orgNpis = [int]$fam.Npis
        $orgMeasured = $true
    } catch { }

    $related = @()
    try { $related = @(Get-RmRelatedOrgNames -Name $Name) } catch { }

    $notes = @(Get-RmMethodologyNotes -Info $info) + @(
        ''
        "ADDRESS-LEVEL METHOD: Care Compare lists which clinicians practice at each street address of '$Name'. Those clinicians bill under their OWN NPIs, so their measured referral volume on $($info.Label) can be summed per address. This is the only way to get per-location figures - an organization NPI carries no service address."
        $(if ($orgMeasured) {
            "COVERAGE: this counts care billed under INDIVIDUAL clinician NPIs. Volume billed under the organization's own NPI(s) ($('{0:N0}' -f $orgVol) patients across $orgNpis NPI(s) here) has no address and is NOT distributed across sites - treat the two as separate views of the same organization."
        } else {
            "COVERAGE: this counts care billed under INDIVIDUAL clinician NPIs. Volume billed under the organization's own NPI(s) could not be measured for this search (no matching organization NPIs were found) - if the organization bills under its own NPI, that volume is additional to the figures here and is NOT distributed across sites."
        })
        $(if ($double -gt 0) { "DOUBLE COUNTING: some clinicians are listed at more than one address, and the data cannot say which visit happened where, so their volume is credited to EACH of their sites. The address rows therefore sum to $('{0:N0}' -f $rowTotal) while the clinicians behind them hold $('{0:N0}' -f $totVol) - $('{0:N0}' -f $double) patients of overlap. AttributedPatients is the de-duplicated figure; per row, SharedSitePatients shows how much of that site's number is also counted elsewhere." })
        'A clinician with no measured volume either bills through the group NPI or had every pair fall under the 11-patient floor; CliniciansWithVolume shows how many of a site''s roster are actually visible.'
        'ROSTER GATE: Care Compare lists only clinicians with an approved Medicare enrollment record and Medicare claims inside a 12-month lookback. It is NOT gated on MIPS - quality-program reporting has no effect on who appears - but cash-pay and non-Medicare clinicians never appear, and historical volume from clinicians who have since left Medicare cannot be placed at an address (measured in one metro market at roughly 18% of individual-therapist volume). The org-NPI and by-NPI views do not have this gate.'
        'Care Compare reflects TODAY''s rosters while the referral data is historical - a clinician who moved is credited to the address they are listed at now.'
        "NAME MATCH: addresses are found by matching '$Name' against Care Compare's facility name. Chains often enroll clinics under a DIFFERENT legal name than the brand - ATI Physical Therapy's clinics are registered as 'ATI HOLDINGS, LLC' - so treat $($byAddr.Count) as the locations matching this search, not necessarily every clinic the brand operates."
        $(if ($related.Count) {
            "RELATED NAMES also in Care Compare, which may be the same company (search them separately to see): " +
            (@($related | ForEach-Object { "$($_.Name) - $($_.Addresses) locations" }) -join '; ') +
            '. These are suggestions from a shared leading word, not a proven link; nothing was merged into the figures above.'
        })
        'RANGE PER SITE: SharedPatients credits a multi-site clinician in full to every site they are listed at, so it is the UPPER bound for a location. ExclusivePatients counts only clinicians who work at that one site, so it is the LOWER bound. A site whose two figures are close is measured cleanly; a wide gap means its number leans on shared staff.'
        $(if ($addrNearMiss.Count) {
            "SPACING NEAR-MISS: $($addrNearMiss.Count) facility name(s) match only when spaces are ignored and were NOT included: " +
            ((@($addrNearMiss) | Select-Object -First 5) -join '; ') +
            $(if ($addrNearMiss.Count -gt 5) { '; ...' }) + '. Search that exact name to include it.'
        })
    ) | Where-Object { $_ }

    [pscustomobject]@{
        Search = $Name
        Year = $info.Year
        Label = $info.Label
        Addresses = @($rows).Count
        AddressesWithVolume = $addrWithVol
        Clinicians = $set.Count
        AttributedPatients = $totVol      # each clinician counted once
        AddressRowTotal = $rowTotal       # what the rows below add up to
        DoubleCountedPatients = $double   # the gap between the two
        OrgNpiPatients = $orgVol
        Chain = Get-RmChainMark $Name
        RelatedNames = @($related)
        Rows = @($rows)
        Notes = @($notes)
    }
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
    'Get-RmZipsInRadius', 'Get-RmUnderservedAreas',
    'Get-RmReferralMap', 'Get-RmInboundByBucket', 'Get-RmGroupBenchmark', 'Get-RmGroupMissedSources',
    'Get-RmGroupTrend',
    'Get-RmProviderReferralActivity',
    'Get-RmProviderTrend', 'Get-RmPracticeBenchmark', 'Get-RmSourceSpecialtyMix',
    'Get-RmReferralGeography', 'Export-RmReferralMapHtml',
    'Get-RmSourceAnalysis', 'Export-RmSourceReportHtml',
    'Get-RmSourceTrend', 'Add-RmSourceTrend',
    'Get-RmCountyMarket', 'Get-RmServiceProfile',
    'Import-RmNppesBulk', 'Import-RmCareCompare', 'Get-RmAffiliatedNpi',
    'Get-RmNppesIndexPath',
    'Get-RmProviderFamily', 'Get-RmLocationReferrals', 'Get-RmRelatedOrgNames',
    'Get-RmChainMark', 'Get-RmChainDetail', 'Get-RmNameSiblingGroups',
    'Import-RmEnrollment', 'Save-RmLocalResources', 'Get-RmLocalDataStatus',
    'Find-RmNppesDisseminationFile',
    'Export-RmResult', 'Clear-RmStaleTemp'
)
