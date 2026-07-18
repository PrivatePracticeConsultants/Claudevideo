# Start-OrderReferringTracker.ps1 — desktop app for the CMS Order & Referring
# eligibility data. Windows only (WPF). Works in Windows PowerShell 5.1 and
# PowerShell 7+. Double-click "Start Order and Referring Tracker.cmd" to run.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

if (-not ($IsWindows -or $env:OS -eq 'Windows_NT')) {
    Write-Error "The graphical app requires Windows. On other systems use the module directly (Import-Module ./OrderReferring; Get-Command -Module OrderReferring)."
    exit 1
}

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

$script:ModulePath = Join-Path $PSScriptRoot 'OrderReferring\OrderReferring.psm1'
$script:RmModulePath = Join-Path $PSScriptRoot 'ReferralMap\ReferralMap.psm1'
Import-Module $script:ModulePath -Force
Import-Module $script:RmModulePath -Force

# ---------------------------------------------------------------------------
# Window layout
# ---------------------------------------------------------------------------

$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Medicare Order &amp; Referring Tracker"
        Width="980" Height="680" MinWidth="820" MinHeight="520"
        WindowStartupLocation="CenterScreen" Background="#F4F6F8">
  <Grid Margin="10">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>

    <!-- Header / status -->
    <Border Grid.Row="0" Background="White" CornerRadius="6" Padding="12" Margin="0,0,0,10"
            BorderBrush="#D5DBE1" BorderThickness="1">
      <Grid>
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel Grid.Column="0">
          <TextBlock Text="Medicare Order &amp; Referring Tracker" FontSize="18" FontWeight="Bold"/>
          <TextBlock x:Name="StatusText" Margin="0,4,0,0" FontSize="13" Foreground="#333"
                     Text="Checking local data..." TextWrapping="Wrap"/>
        </StackPanel>
        <StackPanel Grid.Column="1" Orientation="Vertical" VerticalAlignment="Center">
          <Button x:Name="UpdateButton" Content="Check for updates" Padding="14,7" FontSize="13"/>
          <TextBlock x:Name="UpdateHint" FontSize="11" Foreground="#666" Margin="0,4,0,0"
                     TextAlignment="Center" Text="CMS refreshes ~twice a week"/>
        </StackPanel>
      </Grid>
    </Border>

    <TabControl Grid.Row="1" x:Name="Tabs" Background="White" BorderBrush="#D5DBE1">

      <!-- ============ Search tab ============ -->
      <TabItem Header="  Search providers  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <Grid Grid.Row="0">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0" Orientation="Horizontal">
              <TextBlock Text="Name:" VerticalAlignment="Center" Margin="0,0,6,0"/>
              <TextBox x:Name="NameBox" Width="240" Height="28" VerticalContentAlignment="Center"
                       ToolTip="Last and/or first name, e.g. 'smith john'"/>
              <TextBlock Text="NPI:" VerticalAlignment="Center" Margin="14,0,6,0"/>
              <TextBox x:Name="NpiBox" Width="130" Height="28" VerticalContentAlignment="Center"
                       MaxLength="10" ToolTip="Full or partial (prefix) 10-digit NPI"/>
            </StackPanel>
            <Button Grid.Column="1" x:Name="SearchButton" Content="Search" Padding="18,5"
                    Margin="10,0,0,0" IsDefault="True"/>
            <Button Grid.Column="2" x:Name="ExportSearchButton" Content="Export results..."
                    Padding="12,5" Margin="10,0,0,0" IsEnabled="False"/>
          </Grid>
          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,8,0,8">
            <TextBlock Text="Must be eligible for:" VerticalAlignment="Center" Margin="0,0,8,0"/>
            <CheckBox x:Name="FlagPartB" Content="Part B (incl. outpatient therapy)" Margin="0,0,12,0"/>
            <CheckBox x:Name="FlagDme" Content="DME" Margin="0,0,12,0"/>
            <CheckBox x:Name="FlagHha" Content="Home Health" Margin="0,0,12,0"/>
            <CheckBox x:Name="FlagPmd" Content="PMD" Margin="0,0,12,0"/>
            <CheckBox x:Name="FlagHospice" Content="Hospice"/>
          </StackPanel>
          <DataGrid Grid.Row="2" x:Name="SearchGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="3" x:Name="SearchSummary" Margin="0,6,0,0" Foreground="#333"
                     Text="Enter a name or NPI and click Search."/>
        </Grid>
      </TabItem>

      <!-- ============ Batch check tab ============ -->
      <TabItem Header="  Batch NPI check  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#333" Margin="0,0,0,6"
              Text="Paste NPIs below (any format — one per line, comma-separated, or a whole spreadsheet column), or load a file. Each NPI is validated and checked against the current CMS eligible-to-order/refer list. Use this to verify the referring providers from your own EMR or billing records."/>
          <Grid Grid.Row="1">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBox Grid.Column="0" x:Name="NpiListBox" Height="90" AcceptsReturn="True"
                     VerticalScrollBarVisibility="Auto" TextWrapping="Wrap" FontFamily="Consolas"/>
            <StackPanel Grid.Column="1" Margin="10,0,0,0">
              <Button x:Name="LoadNpiFileButton" Content="Load from file..." Padding="10,5" Margin="0,0,0,6"/>
              <Button x:Name="BatchCheckButton" Content="Run check" Padding="10,5" Margin="0,0,0,6"/>
              <Button x:Name="ExportBatchButton" Content="Export results..." Padding="10,5" IsEnabled="False"/>
            </StackPanel>
          </Grid>
          <DataGrid Grid.Row="2" x:Name="BatchGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal" Margin="0,8,0,0"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="3" x:Name="BatchSummary" Margin="0,6,0,0" Foreground="#333" Text=""/>
        </Grid>
      </TabItem>

      <!-- ============ Changes tab ============ -->
      <TabItem Header="  What changed  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <StackPanel Grid.Row="0" Orientation="Horizontal">
            <TextBlock Text="Compare:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <ComboBox x:Name="OldSnapCombo" Width="200" Height="28"/>
            <TextBlock Text="to" VerticalAlignment="Center" Margin="8,0,8,0"/>
            <ComboBox x:Name="NewSnapCombo" Width="200" Height="28"/>
            <TextBlock Text="Show:" VerticalAlignment="Center" Margin="14,0,6,0"/>
            <ComboBox x:Name="ChangeTypeCombo" Width="110" Height="28" SelectedIndex="0">
              <ComboBoxItem Content="All"/>
              <ComboBoxItem Content="Added"/>
              <ComboBoxItem Content="Removed"/>
              <ComboBoxItem Content="Changed"/>
            </ComboBox>
            <Button x:Name="CompareButton" Content="Compare" Padding="14,5" Margin="10,0,0,0"/>
            <Button x:Name="ExportChangesButton" Content="Export results..." Padding="12,5"
                    Margin="10,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="1" x:Name="ChangesGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal" Margin="0,8,0,0"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="2" x:Name="ChangesSummary" Margin="0,6,0,0" Foreground="#333"
              Text="Snapshots accumulate automatically each time CMS publishes an update. Two or more are needed to compare."/>
        </Grid>
      </TabItem>
      <!-- ============ Referral map tab ============ -->
      <TabItem Header="  Referral map (2015)  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="5*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="6*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#333" Margin="0,0,0,6"
              Text="Enter a ZIP code to see which providers historically fed the most Medicare patients into each outpatient rehab clinic in that area. Built from the newest public CMS shared-patient release (Jan–Sep 2015, 30-day window) joined with the live NPPES registry — it maps the structure of the referral market, not current volumes."/>
          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="ZIP:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="RmZipBox" Width="100" Height="28" VerticalContentAlignment="Center"
                     MaxLength="6" ToolTip="5-digit ZIP, or a prefix like 630* for a wider area"/>
            <CheckBox x:Name="RmOrgOnly" Content="Clinics (organizations) only" VerticalAlignment="Center"
                      Margin="14,0,0,0" ToolTip="Unchecked: also includes individual PT/OT/SLP providers (solo practices bill under individual NPIs)"/>
            <Button x:Name="RmRunButton" Content="Map referral sources" Padding="14,5" Margin="14,0,0,0"/>
            <Button x:Name="RmDownloadButton" Content="Download CMS dataset" Padding="10,5" Margin="10,0,0,0"/>
            <TextBlock x:Name="RmDataStatus" VerticalAlignment="Center" Margin="12,0,0,0" Foreground="#666"/>
          </StackPanel>
          <DataGrid Grid.Row="2" x:Name="RmClinicGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="RmSourceLabel" Text="Referral sources (select a clinic above to filter):"
                       VerticalAlignment="Center"/>
            <Button x:Name="RmExportClinicsButton" Content="Export clinics..." Padding="10,4"
                    Margin="14,0,0,0" IsEnabled="False"/>
            <Button x:Name="RmExportSourcesButton" Content="Export sources..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="RmSourceGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="5" x:Name="RmSummary" Margin="0,6,0,0" Foreground="#333" TextWrapping="Wrap"
              Text="One-time setup: click 'Download CMS dataset' (~356 MB download, ~1.7 GB on disk)."/>
        </Grid>
      </TabItem>
    </TabControl>

    <!-- Footer: honesty note -->
    <Border Grid.Row="2" Background="#FFF7E6" CornerRadius="6" Padding="8" Margin="0,10,0,0"
            BorderBrush="#E8D9A0" BorderThickness="1">
      <TextBlock TextWrapping="Wrap" FontSize="12" Foreground="#5A4A00"
          Text="About this data: the CMS Order &amp; Referring file is an eligibility roster — every provider currently allowed to order/refer for Medicare Part B, DME, Home Health, PMD, and Hospice. It contains no claims and no referral relationships, so it cannot show who referred patients to whom. Use the Batch NPI check with the referral list from your own EMR/billing system to verify and monitor YOUR referring providers."/>
    </Border>
  </Grid>
</Window>
'@

$window = [System.Windows.Markup.XamlReader]::Parse($xaml)
$ui = @{}
foreach ($name in @(
    'StatusText', 'UpdateButton', 'UpdateHint', 'Tabs',
    'NameBox', 'NpiBox', 'SearchButton', 'ExportSearchButton',
    'FlagPartB', 'FlagDme', 'FlagHha', 'FlagPmd', 'FlagHospice', 'SearchGrid', 'SearchSummary',
    'NpiListBox', 'LoadNpiFileButton', 'BatchCheckButton', 'ExportBatchButton', 'BatchGrid', 'BatchSummary',
    'OldSnapCombo', 'NewSnapCombo', 'ChangeTypeCombo', 'CompareButton', 'ExportChangesButton',
    'ChangesGrid', 'ChangesSummary',
    'RmZipBox', 'RmOrgOnly', 'RmRunButton', 'RmDownloadButton', 'RmDataStatus',
    'RmClinicGrid', 'RmSourceLabel', 'RmExportClinicsButton', 'RmExportSourcesButton',
    'RmSourceGrid', 'RmSummary'
)) {
    $ui[$name] = $window.FindName($name)
    if (-not $ui[$name]) { throw "Internal error: UI element '$name' not found." }
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

$script:Data = $null              # in-memory List[OrfProvider] (current snapshot)
$script:LastSearchResults = @()   # full result sets kept for export
$script:LastBatchResults = @()
$script:LastChangeResults = @()
$script:RmResult = $null          # last referral-map result object
$script:Busy = $false
$script:Jobs = New-Object System.Collections.ArrayList
$script:MaxGridRows = 5000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Set-Status([string]$Text) { $ui.StatusText.Text = $Text }

function Set-Busy([bool]$On, [string]$Message) {
    $script:Busy = $On
    foreach ($b in @($ui.UpdateButton, $ui.SearchButton, $ui.BatchCheckButton,
                     $ui.CompareButton, $ui.LoadNpiFileButton,
                     $ui.RmRunButton, $ui.RmDownloadButton)) {
        $b.IsEnabled = -not $On
    }
    $window.Cursor = if ($On) { [System.Windows.Input.Cursors]::Wait } else { $null }
    if ($Message) { Set-Status $Message }
}

function Show-ErrorBox([string]$Message) {
    [void][System.Windows.MessageBox]::Show($window, $Message, 'Order & Referring Tracker',
        [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::Warning)
}

# Runs a script block with parameters in a background runspace so the window
# never freezes; OnDone/OnFail run back on the UI thread via the poll timer.
# Owns the busy choreography: sets busy here, and the timer ALWAYS clears busy
# before dispatching, so no handler can leave the window stuck disabled.
function Invoke-Async {
    param(
        [Parameter(Mandatory)][string]$Kind,
        [Parameter(Mandatory)][string]$WorkerScript,
        [hashtable]$Params = @{},
        [Parameter(Mandatory)][scriptblock]$OnDone,
        [Parameter(Mandatory)][scriptblock]$OnFail,
        [string]$BusyMessage
    )
    Set-Busy $true $BusyMessage
    $rs = [runspacefactory]::CreateRunspace()
    $rs.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($WorkerScript)
    foreach ($k in $Params.Keys) { [void]$ps.AddParameter($k, $Params[$k]) }
    $job = [pscustomobject]@{
        Kind = $Kind; PS = $ps; RS = $rs
        Handle = $ps.BeginInvoke()
        OnDone = $OnDone; OnFail = $OnFail
    }
    [void]$script:Jobs.Add($job)
}

$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(250)
$timer.Add_Tick({
    $done = @($script:Jobs | Where-Object { $_.Handle.IsCompleted })
    foreach ($job in $done) {
        $script:Jobs.Remove($job)
        $result = $null; $failure = $null
        try {
            $output = $job.PS.EndInvoke($job.Handle)
            # A worker SUCCEEDED if it produced output — an incidental
            # non-terminating error record (e.g. an antivirus briefly locking
            # the change-log file) must not discard a completed result.
            if ($output.Count -gt 0) {
                $result = $output
                if ($job.PS.Streams.Error.Count -gt 0) {
                    Write-Warning ("Background task '$($job.Kind)' reported warnings: " +
                        (($job.PS.Streams.Error | ForEach-Object { $_.ToString() }) -join '; '))
                }
            } elseif ($job.PS.Streams.Error.Count -gt 0) {
                $failure = ($job.PS.Streams.Error | ForEach-Object { $_.ToString() }) -join "`n"
            } else {
                $result = $output   # legitimately empty
            }
        } catch {
            $failure = $_.Exception.InnerException.Message
            if (-not $failure) { $failure = $_.Exception.Message }
        } finally {
            $job.PS.Dispose(); $job.RS.Dispose()
        }
        Set-Busy $false $null   # unconditional: handlers can never strand the UI disabled
        try {
            if ($failure) { & $job.OnFail $failure } else { & $job.OnDone $result }
        } catch {
            Show-ErrorBox "Unexpected error: $($_.Exception.Message)"
        }
    }
})
$timer.Start()

# DataGrids bind DataTables (WPF cannot auto-generate columns from PowerShell
# objects). Display is capped; exports always use the full result set.
# Integer-valued columns get typed [int] so clicking a column header sorts
# numerically (a string column would put 9 above 65 above 450).
function ConvertTo-DataTable {
    param([object[]]$Rows, [string[]]$Columns)
    $table = New-Object System.Data.DataTable
    foreach ($c in $Columns) {
        $type = [string]
        if ($Rows.Count -gt 0 -and $Rows[0].$c -is [int]) { $type = [int] }
        [void]$table.Columns.Add($c, $type)
    }
    $count = [Math]::Min($Rows.Count, $script:MaxGridRows)
    for ($i = 0; $i -lt $count; $i++) {
        $r = $table.NewRow()
        foreach ($c in $Columns) {
            $v = $Rows[$i].$c
            $r[$c] = if ($null -eq $v) { [DBNull]::Value } else { $v }
        }
        $table.Rows.Add($r)
    }
    $table
}

function Get-CappedNote([int]$Total) {
    if ($Total -gt $script:MaxGridRows) {
        " Showing first $('{0:N0}' -f $script:MaxGridRows) of $('{0:N0}' -f $Total) rows — exports include all rows."
    } else { '' }
}

function Export-WithDialog {
    param([object[]]$Rows, [string]$SuggestedName, [string]$Description)
    if ($Rows.Count -eq 0) { Show-ErrorBox 'Nothing to export yet — run a search/check first.'; return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'CSV files (*.csv)|*.csv'
    $dialog.FileName = $SuggestedName
    if ($dialog.ShowDialog($window)) {
        try {
            $result = $Rows | Export-OrfResult -Path $dialog.FileName -Description $Description
            Set-Status "Exported $('{0:N0}' -f $result.Rows) rows to $($result.Path) (with methodology sidecar)."
        } catch {
            Show-ErrorBox "Export failed: $($_.Exception.Message)"
        }
    }
}

function Update-SnapshotCombos {
    $files = @(Get-OrfSnapshotFiles)
    $names = @($files | ForEach-Object { $_.Name })
    $ui.OldSnapCombo.ItemsSource = $names
    $ui.NewSnapCombo.ItemsSource = $names
    if ($names.Count -ge 2) {
        $ui.OldSnapCombo.SelectedIndex = $names.Count - 2
        $ui.NewSnapCombo.SelectedIndex = $names.Count - 1
    } elseif ($names.Count -eq 1) {
        $ui.NewSnapCombo.SelectedIndex = 0
    }
}

function Update-StatusFromDisk {
    $status = Get-OrfStatus
    if ($status.LocalRelease) {
        $rowText = if ($status.LocalRowCount) { '{0:N0} providers' -f $status.LocalRowCount } else { 'row count unknown' }
        Set-Status "Data release: $($status.LocalRelease)  |  $rowText  |  $($status.SnapshotCount) snapshot(s) on disk"
    } else {
        Set-Status "No data downloaded yet. Click 'Check for updates' to download the current CMS file (~70 MB)."
    }
    Update-SnapshotCombos
}

function Start-DataLoad {
    $latest = Get-OrfLatestSnapshot
    if (-not $latest) { return }
    Invoke-Async -Kind 'load' -BusyMessage "Loading $($latest.Name) into memory..." `
        -Params @{ ModulePath = $script:ModulePath; Path = $latest.FullName } `
        -WorkerScript 'param($ModulePath, $Path) Import-Module $ModulePath; Import-OrfSnapshot -Path $Path' `
        -OnDone {
            param($result)
            # The worker returns the List as the single output object.
            $script:Data = $result[0]
            Update-StatusFromDisk
        } `
        -OnFail {
            param($message)
            Update-StatusFromDisk
            Show-ErrorBox "Could not load the data file: $message"
        }
}

# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

$ui.UpdateButton.Add_Click({
    if ($script:Busy) { return }
    Invoke-Async -Kind 'update' -Params @{ ModulePath = $script:ModulePath } `
        -BusyMessage 'Contacting CMS and downloading if a newer release exists (this can take a few minutes)...' `
        -WorkerScript 'param($ModulePath) Import-Module $ModulePath; Update-OrfData' `
        -OnDone {
            param($result)
            $r = $result[0]
            Set-Status $r.Message
            Update-SnapshotCombos
            if ($r.Updated -or -not $script:Data) { Start-DataLoad }
        } `
        -OnFail {
            param($message)
            Update-StatusFromDisk
            Show-ErrorBox $message
        }
})

$ui.SearchButton.Add_Click({
    if ($script:Busy) { return }
    if (-not $script:Data) {
        Show-ErrorBox "No data loaded yet. Click 'Check for updates' first to download the CMS file."
        return
    }
    $npi = $ui.NpiBox.Text.Trim()
    if ($npi -and $npi -notmatch '^\d{1,10}$') {
        Show-ErrorBox 'NPI must be digits only (up to 10).'
        return
    }
    $name = $ui.NameBox.Text.Trim()
    $anyFlag = [bool]$ui.FlagPartB.IsChecked -or [bool]$ui.FlagDme.IsChecked -or
               [bool]$ui.FlagHha.IsChecked -or [bool]$ui.FlagPmd.IsChecked -or
               [bool]$ui.FlagHospice.IsChecked
    if (-not $name -and -not $npi -and -not $anyFlag) {
        # An unconstrained search would materialize all ~2M rows on the UI thread.
        Show-ErrorBox 'Enter a name or NPI, or tick at least one eligibility box, then click Search.'
        return
    }
    $hits = [OrfEngine]::Search(
        $script:Data, $name, $npi,
        [bool]$ui.FlagPartB.IsChecked, [bool]$ui.FlagDme.IsChecked,
        [bool]$ui.FlagHha.IsChecked, [bool]$ui.FlagPmd.IsChecked,
        [bool]$ui.FlagHospice.IsChecked, 0)
    # Keep the raw engine hits; only the displayed slice is converted now.
    # Export converts the full set at export time (streams through the module).
    $script:LastSearchResults = $hits
    $display = @($hits | Select-Object -First $script:MaxGridRows | ConvertTo-OrfRecord)
    $cols = @('NPI', 'LastName', 'FirstName', 'PartB', 'DME', 'HHA', 'PMD', 'Hospice')
    $ui.SearchGrid.ItemsSource = (ConvertTo-DataTable -Rows $display -Columns $cols).DefaultView
    $ui.ExportSearchButton.IsEnabled = ($hits.Count -gt 0)
    $ui.SearchSummary.Text = "$('{0:N0}' -f $hits.Count) matching provider(s)." + (Get-CappedNote $hits.Count)
})

$ui.ExportSearchButton.Add_Click({
    $filters = @()
    if ($ui.NameBox.Text) { $filters += "name contains '$($ui.NameBox.Text)'" }
    if ($ui.NpiBox.Text) { $filters += "NPI starts with '$($ui.NpiBox.Text)'" }
    foreach ($pair in @(@($ui.FlagPartB, 'PARTB'), @($ui.FlagDme, 'DME'), @($ui.FlagHha, 'HHA'),
                        @($ui.FlagPmd, 'PMD'), @($ui.FlagHospice, 'HOSPICE'))) {
        if ($pair[0].IsChecked) { $filters += "requires $($pair[1])=Y" }
    }
    $desc = 'Provider search'
    if ($filters.Count -gt 0) { $desc += ': ' + ($filters -join '; ') }
    Export-WithDialog -Rows @($script:LastSearchResults | ConvertTo-OrfRecord) `
        -SuggestedName 'provider-search.csv' -Description $desc
})

$ui.LoadNpiFileButton.Add_Click({
    $dialog = New-Object Microsoft.Win32.OpenFileDialog
    $dialog.Filter = 'Text or CSV files (*.txt;*.csv)|*.txt;*.csv|All files (*.*)|*.*'
    if ($dialog.ShowDialog($window)) {
        try {
            $npis = @(Get-OrfNpiFromText -Text (Get-Content -LiteralPath $dialog.FileName -Raw))
            if ($npis.Count -eq 0) {
                Show-ErrorBox "No 10-digit NPIs found in $($dialog.FileName)."
                return
            }
            $ui.NpiListBox.Text = $npis -join "`r`n"
            $ui.BatchSummary.Text = "Loaded $($npis.Count) unique NPI(s) from file. Click 'Run check'."
        } catch {
            Show-ErrorBox "Could not read the file: $($_.Exception.Message)"
        }
    }
})

$ui.BatchCheckButton.Add_Click({
    if ($script:Busy) { return }
    if (-not $script:Data) {
        Show-ErrorBox "No data loaded yet. Click 'Check for updates' first to download the CMS file."
        return
    }
    $npis = @(Get-OrfNpiFromText -Text $ui.NpiListBox.Text)
    if ($npis.Count -eq 0) {
        Show-ErrorBox 'Paste at least one 10-digit NPI (or load a file) first.'
        return
    }
    # One classification implementation for GUI and CLI: the module function,
    # fed the already-loaded snapshot (C#-indexed — no file reload, no drift).
    $records = @($npis | Test-OrfNpi -Data $script:Data)
    $script:LastBatchResults = $records
    $cols = @('NPI', 'Status', 'LastName', 'FirstName', 'PartB', 'DME', 'HHA', 'PMD', 'Hospice')
    $ui.BatchGrid.ItemsSource = (ConvertTo-DataTable -Rows $records -Columns $cols).DefaultView
    $ui.ExportBatchButton.IsEnabled = $true
    $eligible = @($records | Where-Object Status -like 'ELIGIBLE*').Count
    $notFound = @($records | Where-Object Status -eq 'NOT ON LIST').Count
    $invalid  = @($records | Where-Object Status -eq 'INVALID NPI').Count
    $ui.BatchSummary.Text = ("Checked $($records.Count) NPI(s): $eligible eligible, " +
        "$notFound not on the CMS list, $invalid invalid." + (Get-CappedNote $records.Count))
})

$ui.ExportBatchButton.Add_Click({
    Export-WithDialog -Rows $script:LastBatchResults -SuggestedName 'npi-eligibility-check.csv' `
        -Description "Batch NPI eligibility check ($(@($script:LastBatchResults).Count) NPIs)"
})

$ui.CompareButton.Add_Click({
    if ($script:Busy) { return }
    $files = @(Get-OrfSnapshotFiles)
    if ($files.Count -lt 2) {
        Show-ErrorBox ("Two snapshots are needed to compare, but only $($files.Count) exist. " +
            "They accumulate automatically each time CMS publishes an update (about twice a week), " +
            "as long as you check for updates or keep the scheduled task installed.")
        return
    }
    $oldName = [string]$ui.OldSnapCombo.SelectedItem
    $newName = [string]$ui.NewSnapCombo.SelectedItem
    if (-not $oldName -or -not $newName) { Show-ErrorBox 'Pick two snapshots to compare.'; return }
    if ($oldName -eq $newName) { Show-ErrorBox 'Pick two different snapshots.'; return }
    $snapDir = Split-Path $files[0].FullName
    Invoke-Async -Kind 'compare' -BusyMessage "Comparing $oldName to $newName..." `
        -Params @{
            ModulePath = $script:ModulePath
            OldPath = (Join-Path $snapDir $oldName)
            NewPath = (Join-Path $snapDir $newName)
        } `
        -WorkerScript 'param($ModulePath, $OldPath, $NewPath) Import-Module $ModulePath; Compare-OrfSnapshot -OldPath $OldPath -NewPath $NewPath' `
        -OnDone {
            param($result)
            Update-StatusFromDisk
            $all = @($result)
            $filter = ([System.Windows.Controls.ComboBoxItem]$ui.ChangeTypeCombo.SelectedItem).Content
            $records = if ($filter -and $filter -ne 'All') {
                @($all | Where-Object ChangeType -eq $filter)
            } else { $all }
            $script:LastChangeResults = $records
            $cols = @('ChangeType', 'NPI', 'LastName', 'FirstName', 'OldFlags', 'NewFlags')
            $ui.ChangesGrid.ItemsSource = (ConvertTo-DataTable -Rows $records -Columns $cols).DefaultView
            $ui.ExportChangesButton.IsEnabled = ($records.Count -gt 0)
            $added   = @($all | Where-Object ChangeType -eq 'Added').Count
            $removed = @($all | Where-Object ChangeType -eq 'Removed').Count
            $changed = @($all | Where-Object ChangeType -eq 'Changed').Count
            $ui.ChangesSummary.Text = ("$('{0:N0}' -f $added) added, $('{0:N0}' -f $removed) removed, " +
                "$('{0:N0}' -f $changed) changed between the two snapshots." + (Get-CappedNote $records.Count))
        } `
        -OnFail {
            param($message)
            Update-StatusFromDisk
            Show-ErrorBox "Comparison failed: $message"
        }
})

$ui.ExportChangesButton.Add_Click({
    $desc = "Changes between snapshots $([string]$ui.OldSnapCombo.SelectedItem) and $([string]$ui.NewSnapCombo.SelectedItem)"
    Export-WithDialog -Rows $script:LastChangeResults -SuggestedName 'orf-changes.csv' -Description $desc
})

# ---------------------------------------------------------------------------
# Referral map tab
# ---------------------------------------------------------------------------

$script:RmClinicCols = @('NPI', 'Name', 'Type', 'Taxonomy', 'City', 'State', 'Zip',
                         'ReferralSources', 'SharedPatients', 'SameDay', 'ExistedInDataYear')
$script:RmSourceCols = @('SourceNPI', 'SourceName', 'SourceSpecialty', 'SourceCity', 'SourceState',
                         'ClinicNPI', 'ClinicName', 'SharedPatients', 'SharedEvents', 'SameDay')

function Update-RmStatus {
    $status = Get-RmStatus
    if ($status.DatasetReady) {
        $ui.RmDataStatus.Text = 'Dataset ready ({0}, {1}-day window)' -f $status.Year, $status.Interval
        $ui.RmDownloadButton.Visibility = 'Collapsed'
    } else {
        $ui.RmDataStatus.Text = 'Dataset not downloaded yet'
        $ui.RmDownloadButton.Visibility = 'Visible'
    }
}

function Export-RmWithDialog {
    param([object[]]$Rows, [string]$SuggestedName, [string]$Description)
    if (-not $Rows -or $Rows.Count -eq 0) { Show-ErrorBox 'Nothing to export yet — run a map first.'; return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'CSV files (*.csv)|*.csv'
    $dialog.FileName = $SuggestedName
    if ($dialog.ShowDialog($window)) {
        try {
            $notes = if ($script:RmResult) { $script:RmResult.Notes } else { @() }
            $result = $Rows | Export-RmResult -Path $dialog.FileName -Notes $notes -Description $Description
            Set-Status "Exported $('{0:N0}' -f $result.Rows) rows to $($result.Path) (with methodology sidecar)."
        } catch {
            Show-ErrorBox "Export failed: $($_.Exception.Message)"
        }
    }
}

$ui.RmDownloadButton.Add_Click({
    if ($script:Busy) { return }
    $ui.RmSummary.Text = 'Downloading... this tab will report when the dataset is ready.'
    Invoke-Async -Kind 'rm-download' -Params @{ RmModulePath = $script:RmModulePath } `
        -BusyMessage 'Downloading the CMS shared-patient dataset (~356 MB; this can take several minutes)...' `
        -WorkerScript 'param($RmModulePath) Import-Module $RmModulePath; Save-RmDataset' `
        -OnDone {
            param($result)
            Update-StatusFromDisk
            Update-RmStatus
            $ui.RmSummary.Text = $result[0].Message + ' Enter a ZIP and click "Map referral sources".'
        } `
        -OnFail {
            param($message)
            Update-StatusFromDisk
            Update-RmStatus
            Show-ErrorBox $message
        }
})

$ui.RmRunButton.Add_Click({
    if ($script:Busy) { return }
    $zip = $ui.RmZipBox.Text.Trim()
    if ($zip -notmatch '^\d{3,5}\*?$' -or ($zip -match '^\d{1,4}$' -and $zip.Length -lt 5)) {
        Show-ErrorBox 'Enter a 5-digit ZIP code, or a prefix ending in * (e.g. 630*) for a wider area.'
        return
    }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox "The CMS dataset isn't downloaded yet — click 'Download CMS dataset' first (one-time, ~356 MB)."
        return
    }
    Invoke-Async -Kind 'rm-run' -Params @{
            RmModulePath = $script:RmModulePath
            Zip = $zip
            OrgOnly = [bool]$ui.RmOrgOnly.IsChecked
        } `
        -BusyMessage "Mapping referral sources for $zip — NPPES lookup, then a scan of ~35M provider pairs (1-2 minutes)..." `
        -WorkerScript 'param($RmModulePath, $Zip, $OrgOnly) Import-Module $RmModulePath; Get-RmReferralMap -Zip $Zip -OrganizationsOnly:$OrgOnly' `
        -OnDone {
            param($result)
            $map = $result[0]
            $script:RmResult = $map
            $clinics = @($map.Clinics)
            $sources = @($map.Sources)
            $ui.RmClinicGrid.ItemsSource = (ConvertTo-DataTable -Rows $clinics -Columns $script:RmClinicCols).DefaultView
            $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows $sources -Columns $script:RmSourceCols).DefaultView
            $ui.RmSourceLabel.Text = 'Referral sources — all clinics (select a clinic above to filter):'
            $ui.RmExportClinicsButton.IsEnabled = ($clinics.Count -gt 0)
            $ui.RmExportSourcesButton.IsEnabled = ($sources.Count -gt 0)
            $withVolume = @($clinics | Where-Object { $_.SharedPatients -gt 0 }).Count
            $tooNew = @($clinics | Where-Object { $_.ExistedInDataYear -like 'No*' }).Count
            $ui.RmSummary.Text = ("ZIP $($map.Zip): $($clinics.Count) rehab provider(s) found in NPPES; " +
                "$withVolume had inbound shared-patient volume in the 2015 data " +
                "($('{0:N0}' -f $sources.Count) source relationships)." +
                $(if ($tooNew -gt 0) { " $tooNew did not have an NPI yet in 2015 (their zeros mean 'did not exist', not 'no referrals')." } else { '' }) +
                ' Reminder: 2015 vintage — market structure, not current volumes; pairs under 11 patients/year are excluded by CMS. Tip: a prefix like 630* widens the area.')
            Set-Status "Referral map for $($map.Zip) complete."
        } `
        -OnFail {
            param($message)
            Update-RmStatus
            Show-ErrorBox $message
        }
})

$ui.RmClinicGrid.Add_SelectionChanged({
    if (-not $script:RmResult) { return }
    $row = $ui.RmClinicGrid.SelectedItem
    if ($row -is [System.Data.DataRowView]) {
        $npi = [string]$row.Row['NPI']
        $filtered = @($script:RmResult.Sources | Where-Object { $_.ClinicNPI -eq $npi })
        $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows $filtered -Columns $script:RmSourceCols).DefaultView
        $ui.RmSourceLabel.Text = "Referral sources for $([string]$row.Row['Name']) ($npi) — $($filtered.Count) source(s):"
    } else {
        $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows @($script:RmResult.Sources) -Columns $script:RmSourceCols).DefaultView
        $ui.RmSourceLabel.Text = 'Referral sources — all clinics (select a clinic above to filter):'
    }
})

$ui.RmExportClinicsButton.Add_Click({
    if (-not $script:RmResult) { return }
    Export-RmWithDialog -Rows @($script:RmResult.Clinics) `
        -SuggestedName "rehab-clinics-$($script:RmResult.Zip.TrimEnd('*')).csv" `
        -Description "Outpatient rehab providers in ZIP $($script:RmResult.Zip), ranked by inbound shared-patient volume (CMS 2015 shared-patient data)"
})

$ui.RmExportSourcesButton.Add_Click({
    if (-not $script:RmResult) { return }
    Export-RmWithDialog -Rows @($script:RmResult.Sources) `
        -SuggestedName "referral-sources-$($script:RmResult.Zip.TrimEnd('*')).csv" `
        -Description "Referral sources feeding outpatient rehab providers in ZIP $($script:RmResult.Zip) (CMS 2015 shared-patient data)"
})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

Update-StatusFromDisk
Update-RmStatus
if (Get-OrfLatestSnapshot) { Start-DataLoad }

$window.Add_Closed({ $timer.Stop() })
[void]$window.ShowDialog()
