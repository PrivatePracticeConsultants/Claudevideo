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
$script:PgModulePath = Join-Path $PSScriptRoot 'PracticeGroups\PracticeGroups.psm1'
Import-Module $script:ModulePath -Force
Import-Module $script:RmModulePath -Force
Import-Module $script:PgModulePath -Force

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
            <ComboBox x:Name="ChangeTypeCombo" Width="120" Height="28" SelectedIndex="0">
              <ComboBoxItem Content="All"/>
              <ComboBoxItem Content="Added"/>
              <ComboBoxItem Content="Removed"/>
              <ComboBoxItem Content="Changed"/>
              <ComboBoxItem Content="Renamed"/>
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
            <Button x:Name="RmExportMixButton" Content="Export specialty mix..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"
                    ToolTip="Break the displayed referral sources down by specialty (e.g. % from ortho vs primary care)."/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="RmSourceGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="5" x:Name="RmSummary" Margin="0,6,0,0" Foreground="#333" TextWrapping="Wrap"
              Text="One-time setup: click 'Download CMS dataset' (~356 MB download, ~1.7 GB on disk)."/>
        </Grid>
      </TabItem>

      <!-- ============ Practice groups tab ============ -->
      <TabItem Header="  Practice groups  ">
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
              Text="Enter a ZIP to see the outpatient-rehab PRACTICE GROUPS operating there, each with its therapist roster. Built from the CMS clinic-group reassignment file (current, updated ~monthly) joined with the live NPPES registry. This is who practices where NOW — it fills the gap where private-practice clinics were invisible in the referral-map tab."/>
          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="ZIP:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="PgZipBox" Width="100" Height="28" VerticalContentAlignment="Center"
                     MaxLength="6" ToolTip="5-digit ZIP, or a prefix like 630* for a wider area"/>
            <Button x:Name="PgRunButton" Content="Find practice groups" Padding="14,5" Margin="14,0,0,0"/>
            <Button x:Name="PgDownloadButton" Content="Download CMS dataset" Padding="10,5" Margin="10,0,0,0"/>
            <TextBlock x:Name="PgDataStatus" VerticalAlignment="Center" Margin="12,0,0,0" Foreground="#666"/>
          </StackPanel>
          <DataGrid Grid.Row="2" x:Name="PgGroupGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="PgRosterLabel" Text="Therapist roster (select a group above to filter):"
                       VerticalAlignment="Center"/>
            <Button x:Name="PgFootprintButton" Content="Add 2015 referral footprint" Padding="10,4"
                    Margin="14,0,0,0" IsEnabled="False"
                    ToolTip="Roll each group's LOCAL therapists' 2015 shared-patient volume up to the group (needs the Referral map dataset)."/>
            <Button x:Name="PgExportGroupsButton" Content="Export groups..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"/>
            <Button x:Name="PgExportRosterButton" Content="Export rosters..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="PgRosterGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="5" x:Name="PgSummary" Margin="0,6,0,0" Foreground="#333" TextWrapping="Wrap"
              Text="One-time setup: click 'Download CMS dataset' (~510 MB download). This tab needs no other data."/>
        </Grid>
      </TabItem>

      <!-- ============ Provider 360 lookup tab ============ -->
      <TabItem Header="  Provider lookup  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#333" Margin="0,0,0,6"
              Text="Enter any NPI for a single-provider profile that pulls together every dataset in this app: current eligibility and specialty, practice-group memberships, and their 2015 referral activity (who sent them patients, and who they sent onward). Optional data is shown when downloaded on the other tabs."/>
          <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="NPI:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="LkNpiBox" Width="140" Height="28" VerticalContentAlignment="Center"
                     MaxLength="10" ToolTip="A full 10-digit NPI"/>
            <Button x:Name="LkRunButton" Content="Look up provider" Padding="14,5" Margin="14,0,0,0"/>
          </StackPanel>
          <Border Grid.Row="2" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="10" Margin="0,0,0,8">
            <TextBlock x:Name="LkDetail" TextWrapping="Wrap" Foreground="#222"
                       Text="Enter an NPI and click Look up provider."/>
          </Border>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,4,0,4">
            <TextBlock x:Name="LkInboundLabel" VerticalAlignment="Center"
                       Text="Referral sources (who shared patients INTO them, 2015):"/>
            <Button x:Name="LkExportInboundButton" Content="Export..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="LkInboundGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="5" Orientation="Horizontal" Margin="0,4,0,4">
            <TextBlock x:Name="LkOutboundLabel" VerticalAlignment="Center"
                       Text="Referral destinations (who they shared patients ONWARD to, 2015):"/>
            <Button x:Name="LkExportOutboundButton" Content="Export..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="6" x:Name="LkOutboundGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
        </Grid>
      </TabItem>

      <!-- ============ Watchlist tab ============ -->
      <TabItem Header="  Watchlist  ">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#333" Margin="0,0,0,6"
              Text="Save your referring providers' NPIs once, then after each bi-weekly CMS update click Check to see — for just YOUR referrers — their current eligibility and what changed since the previous update (dropped, flags flipped, or renamed). Turns the one-time batch check into ongoing monitoring."/>
          <Grid Grid.Row="1" Margin="0,0,0,6">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBox Grid.Column="0" x:Name="WlBox" Height="70" AcceptsReturn="True"
                     VerticalScrollBarVisibility="Auto" TextWrapping="Wrap" FontFamily="Consolas"
                     ToolTip="Paste your referring providers' NPIs (any format)."/>
            <StackPanel Grid.Column="1" Margin="10,0,0,0">
              <Button x:Name="WlSaveButton" Content="Save watchlist" Padding="10,5" Margin="0,0,0,6"/>
              <Button x:Name="WlCheckButton" Content="Check now" Padding="10,5" Margin="0,0,0,6"/>
              <Button x:Name="WlExportButton" Content="Export report..." Padding="10,5" IsEnabled="False"/>
            </StackPanel>
          </Grid>
          <DataGrid Grid.Row="2" x:Name="WlGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <TextBlock Grid.Row="3" x:Name="WlSummary" Margin="0,6,0,0" Foreground="#333" TextWrapping="Wrap"
              Text="Paste NPIs, click Save watchlist, then Check now. Needs the Order &amp; Referring data (Search tab) downloaded."/>
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
    'RmSourceGrid', 'RmSummary',
    'PgZipBox', 'PgRunButton', 'PgDownloadButton', 'PgDataStatus', 'PgGroupGrid',
    'PgRosterLabel', 'PgFootprintButton', 'PgExportGroupsButton', 'PgExportRosterButton',
    'PgRosterGrid', 'PgSummary',
    'LkNpiBox', 'LkRunButton', 'LkDetail', 'LkInboundLabel', 'LkExportInboundButton',
    'LkInboundGrid', 'LkOutboundLabel', 'LkExportOutboundButton', 'LkOutboundGrid',
    'RmExportMixButton',
    'WlBox', 'WlSaveButton', 'WlCheckButton', 'WlExportButton', 'WlGrid', 'WlSummary'
)) {
    $ui[$name] = $window.FindName($name)
    if (-not $ui[$name]) { throw "Internal error: UI element '$name' not found." }
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

$script:Data = $null              # in-memory List[OrfProvider] (current snapshot)
$script:LoadedSnapshotFile = $null # the snapshot file $script:Data came from (for export provenance)
$script:PendingLoadFile = $null   # file a background load is currently reading
$script:LastSearchResults = @()   # full result sets kept for export
$script:LastSearchDesc = ''       # description captured when the search ran (not at export time)
$script:LastBatchResults = @()
$script:LastChangeResultsAll = @() # unfiltered compare result (ChangeTypeCombo filters a view of this)
$script:LastChangeResults = @()    # currently-shown (filtered) compare result
$script:LastChangeDesc = ''
$script:RmResult = $null          # last referral-map result object
$script:PgResult = $null          # last practice-group result object
$script:LkInbound = @()           # last provider-lookup inbound / outbound rows
$script:LkOutbound = @()
$script:LkNpi = ''
$script:WlReport = @()            # last watchlist report rows
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
                     $ui.RmRunButton, $ui.RmDownloadButton,
                     $ui.PgRunButton, $ui.PgDownloadButton, $ui.PgFootprintButton,
                     $ui.LkRunButton, $ui.WlCheckButton)) {
        $b.IsEnabled = -not $On
    }
    $window.Cursor = if ($On) { [System.Windows.Input.Cursors]::Wait } else { $null }
    if ($Message) { Set-Status $Message }
}

function Show-ErrorBox([string]$Message) {
    [void][System.Windows.MessageBox]::Show($window, $Message, 'Medicare Order & Referring Tracker',
        [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::Warning)
}

# Yes/No confirmation, used before the big one-time dataset downloads.
function Confirm-Box([string]$Message) {
    ([System.Windows.MessageBox]::Show($window, $Message, 'Medicare Order & Referring Tracker',
        [System.Windows.MessageBoxButton]::YesNo, [System.Windows.MessageBoxImage]::Question)) -eq
        [System.Windows.MessageBoxResult]::Yes
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
    # Return with a leading comma: PowerShell otherwise ENUMERATES the DataTable
    # into its DataRows on output, so the caller's `.DefaultView` would run
    # against a stream of DataRows and WPF's grid bind would fail with
    # "Value cannot be null. Parameter name: key". The comma keeps it a DataTable.
    , $table
}

function Get-CappedNote([int]$Total) {
    if ($Total -gt $script:MaxGridRows) {
        " Showing first $('{0:N0}' -f $script:MaxGridRows) of $('{0:N0}' -f $Total) rows — exports include all rows."
    } else { '' }
}

function Export-WithDialog {
    param([object[]]$Rows, [string]$SuggestedName, [string]$Description)
    if ($script:Busy) { return }
    if (-not $Rows -or $Rows.Count -eq 0) { Show-ErrorBox 'Nothing to export yet — run a search/check first.'; return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'CSV files (*.csv)|*.csv'
    $dialog.FileName = $SuggestedName
    if ($dialog.ShowDialog($window)) {
        try {
            # Pass the exact snapshot the shown rows came from, so the sidecar
            # cites that release even if a newer one was downloaded since.
            $result = $Rows | Export-OrfResult -Path $dialog.FileName -Description $Description `
                -DataSnapshotFile $script:LoadedSnapshotFile
            Set-Status "Exported $('{0:N0}' -f $result.Rows) rows to $($result.Path) (with methodology sidecar)."
        } catch {
            Show-ErrorBox "Export failed: $($_.Exception.Message)"
        }
    }
}

function Update-SnapshotCombos {
    # Preserve the user's current picks across refreshes — resetting them mid-task
    # would silently change which two snapshots a pending Compare/Export refers to.
    $files = @(Get-OrfSnapshotFiles)
    $names = @($files | ForEach-Object { $_.Name })
    $prevOld = [string]$ui.OldSnapCombo.SelectedItem
    $prevNew = [string]$ui.NewSnapCombo.SelectedItem
    $ui.OldSnapCombo.ItemsSource = $names
    $ui.NewSnapCombo.ItemsSource = $names
    if ($prevOld -and $names -contains $prevOld) { $ui.OldSnapCombo.SelectedItem = $prevOld }
    elseif ($names.Count -ge 2) { $ui.OldSnapCombo.SelectedIndex = $names.Count - 2 }
    if ($prevNew -and $names -contains $prevNew) { $ui.NewSnapCombo.SelectedItem = $prevNew }
    elseif ($names.Count -ge 1) { $ui.NewSnapCombo.SelectedIndex = $names.Count - 1 }
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

function Reset-SearchAndBatch {
    # Results computed from a now-replaced snapshot must not be exportable —
    # their sidecar provenance would no longer match the loaded data.
    $script:LastSearchResults = @(); $script:LastBatchResults = @()
    $ui.ExportSearchButton.IsEnabled = $false
    $ui.ExportBatchButton.IsEnabled = $false
    $ui.SearchGrid.ItemsSource = $null
    $ui.BatchGrid.ItemsSource = $null
}

function Start-DataLoad {
    $latest = Get-OrfLatestSnapshot
    if (-not $latest) { return }
    # Record the file we are about to load in a script-scoped var (not a closure
    # over a function local, which would not survive the async boundary).
    $script:PendingLoadFile = $latest.FullName
    Invoke-Async -Kind 'load' -BusyMessage "Loading $($latest.Name) into memory..." `
        -Params @{ ModulePath = $script:ModulePath; Path = $latest.FullName } `
        -WorkerScript 'param($ModulePath, $Path) Import-Module $ModulePath; Import-OrfSnapshot -Path $Path' `
        -OnDone {
            param($result)
            # The worker returns the List as the single output object.
            $script:Data = $result[0]
            $script:LoadedSnapshotFile = $script:PendingLoadFile
            Reset-SearchAndBatch   # old results belonged to the previous snapshot
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
    # Capture the description NOW so a later edit to the boxes can't relabel the
    # already-computed rows at export time.
    $filters = @()
    if ($name) { $filters += "name contains '$name'" }
    if ($npi)  { $filters += "NPI starts with '$npi'" }
    foreach ($pair in @(@($ui.FlagPartB, 'PARTB'), @($ui.FlagDme, 'DME'), @($ui.FlagHha, 'HHA'),
                        @($ui.FlagPmd, 'PMD'), @($ui.FlagHospice, 'HOSPICE'))) {
        if ($pair[0].IsChecked) { $filters += "requires $($pair[1])=Y" }
    }
    $script:LastSearchDesc = 'Provider search' + $(if ($filters.Count) { ': ' + ($filters -join '; ') } else { '' })
    $display = @($hits | Select-Object -First $script:MaxGridRows | ConvertTo-OrfRecord)
    $cols = @('NPI', 'LastName', 'FirstName', 'PartB', 'DME', 'HHA', 'PMD', 'Hospice')
    $ui.SearchGrid.ItemsSource = (ConvertTo-DataTable -Rows $display -Columns $cols).DefaultView
    $ui.ExportSearchButton.IsEnabled = ($hits.Count -gt 0)
    $ui.SearchSummary.Text = "$('{0:N0}' -f $hits.Count) matching provider(s)." + (Get-CappedNote $hits.Count)
})

$ui.ExportSearchButton.Add_Click({
    Export-WithDialog -Rows @($script:LastSearchResults | ConvertTo-OrfRecord) `
        -SuggestedName 'provider-search.csv' -Description $script:LastSearchDesc
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
    # Capture the description synchronously (the OnDone closure must not depend
    # on these click-handler locals surviving the async boundary).
    $script:LastChangeDesc = "Changes between snapshots $oldName and $newName"
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
            $script:LastChangeResultsAll = @($result)
            Update-ChangesView   # applies the current ChangeType filter to the grid + export set
        } `
        -OnFail {
            param($message)
            Update-StatusFromDisk
            Show-ErrorBox "Comparison failed: $message"
        }
})

# Re-applies the ChangeType filter to the stored full compare result. Called by
# Compare's OnDone AND by the ChangeType combo, so flipping the combo after a
# compare actually re-filters instead of leaving a stale grid/export set.
function Update-ChangesView {
    $all = @($script:LastChangeResultsAll)
    $sel = $ui.ChangeTypeCombo.SelectedItem
    $filter = if ($sel -is [System.Windows.Controls.ComboBoxItem]) { [string]$sel.Content } else { 'All' }
    $records = if ($filter -and $filter -ne 'All') { @($all | Where-Object ChangeType -eq $filter) } else { $all }
    $script:LastChangeResults = $records
    $cols = @('ChangeType', 'NPI', 'LastName', 'FirstName', 'OldName', 'OldFlags', 'NewFlags')
    $ui.ChangesGrid.ItemsSource = (ConvertTo-DataTable -Rows $records -Columns $cols).DefaultView
    $ui.ExportChangesButton.IsEnabled = ($records.Count -gt 0)
    $added   = @($all | Where-Object ChangeType -eq 'Added').Count
    $removed = @($all | Where-Object ChangeType -eq 'Removed').Count
    $changed = @($all | Where-Object ChangeType -eq 'Changed').Count
    $renamed = @($all | Where-Object ChangeType -eq 'Renamed').Count
    $ui.ChangesSummary.Text = ("$('{0:N0}' -f $added) added, $('{0:N0}' -f $removed) removed, " +
        "$('{0:N0}' -f $changed) flag-changed, $('{0:N0}' -f $renamed) renamed between the two snapshots." +
        (Get-CappedNote $records.Count))
}

$ui.ChangeTypeCombo.Add_SelectionChanged({ if ($script:LastChangeResultsAll.Count) { Update-ChangesView } })

$ui.ExportChangesButton.Add_Click({
    Export-WithDialog -Rows $script:LastChangeResults -SuggestedName 'orf-changes.csv' `
        -Description $script:LastChangeDesc
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
    param([object[]]$Rows, [string]$SuggestedName, [string]$Description, [string[]]$Notes)
    if ($script:Busy) { return }
    if (-not $Rows -or $Rows.Count -eq 0) { Show-ErrorBox 'Nothing to export yet — run a map first.'; return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'CSV files (*.csv)|*.csv'
    $dialog.FileName = $SuggestedName
    if ($dialog.ShowDialog($window)) {
        try {
            $useNotes = if ($PSBoundParameters.ContainsKey('Notes')) { $Notes }
                        elseif ($script:RmResult) { $script:RmResult.Notes } else { @() }
            $result = $Rows | Export-RmResult -Path $dialog.FileName -Notes $useNotes -Description $Description
            Set-Status "Exported $('{0:N0}' -f $result.Rows) rows to $($result.Path) (with methodology sidecar)."
        } catch {
            Show-ErrorBox "Export failed: $($_.Exception.Message)"
        }
    }
}

# Methodology notes for a Provider 360 referral-activity export.
$script:LkNotes = @(
    'Source: CMS Physician Shared Patient Patterns (FOIA), 2015, 30-day interval.'
    '2015 vintage — market structure, not current volumes.'
    'SharedPatients = unique Medicare beneficiaries shared in the interval window (a referral proxy, not billed referrals). Labs/imaging/hospitals appear from co-occurring care — read by specialty.'
    'Inbound = the other provider was seen FIRST (they shared into this NPI). Outbound = this NPI was seen first. Same-day pairs are attributed by CMS to the lower NPI, so direction near same-day is approximate.'
)

$ui.RmDownloadButton.Add_Click({
    if ($script:Busy) { return }
    if (-not (Confirm-Box ("This one-time download is about 356 MB and needs roughly 1.7 GB " +
        "of free disk space while it unpacks. It can take several minutes on a slow connection." +
        "`n`nStart the download now?"))) { return }
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
            $ui.RmSummary.Text = 'Download did not complete. Click "Download CMS dataset" to try again.'
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
            $ui.RmExportMixButton.IsEnabled = ($sources.Count -gt 0)
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

$ui.RmExportMixButton.Add_Click({
    if (-not $script:RmResult) { return }
    # Break the referral sources down by specialty. If a clinic is selected,
    # profile just that clinic's sources; otherwise the whole ZIP.
    $sel = $ui.RmClinicGrid.SelectedItem
    $rows = @($script:RmResult.Sources)
    $scope = "ZIP $($script:RmResult.Zip)"
    if ($sel -is [System.Data.DataRowView]) {
        $npi = [string]$sel.Row['NPI']
        $rows = @($rows | Where-Object { $_.ClinicNPI -eq $npi })
        $scope = "$([string]$sel.Row['Name']) ($npi)"
    }
    $mix = @(Get-RmSourceSpecialtyMix -Rows $rows)
    $notes = @($script:RmResult.Notes) + @('',
        'Specialty mix: referral sources grouped by NPPES primary specialty. PctOfVolume is each specialty''s share of total shared-patient volume. Read with the usual caveat — labs/imaging/hospitals appear as "sources" from co-occurring care.')
    Export-RmWithDialog -Rows $mix -SuggestedName "specialty-mix-$($script:RmResult.Zip.TrimEnd('*')).csv" `
        -Description "Referral-source specialty mix for $scope (CMS 2015 shared-patient data)" -Notes $notes
})

# ---------------------------------------------------------------------------
# Practice groups tab
# ---------------------------------------------------------------------------

$script:PgGroupCols = @('GroupName', 'State', 'TherapistsInZip', 'RosterSize', 'LocalReferrals2015', 'GroupPacId')
$script:PgRosterCols = @('GroupName', 'GroupPacId', 'NPI', 'FirstName', 'LastName', 'Specialty', 'InThisZip')
$script:PgFootprintSources = @{}   # groupName -> top-sources array (after footprint run)

function Update-PgStatus {
    $status = Get-PgStatus
    if ($status.DatasetReady) {
        $ui.PgDataStatus.Text = 'Dataset ready ({0:N0} MB)' -f ($status.DatasetBytes / 1MB)
        $ui.PgDownloadButton.Visibility = 'Collapsed'
    } else {
        $ui.PgDataStatus.Text = 'Dataset not downloaded yet'
        $ui.PgDownloadButton.Visibility = 'Visible'
    }
}

function Export-PgWithDialog {
    param([object[]]$Rows, [string]$SuggestedName, [string]$Description)
    if ($script:Busy) { return }
    if (-not $Rows -or $Rows.Count -eq 0) { Show-ErrorBox 'Nothing to export yet — find groups first.'; return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'CSV files (*.csv)|*.csv'
    $dialog.FileName = $SuggestedName
    if ($dialog.ShowDialog($window)) {
        try {
            $notes = if ($script:PgResult) { $script:PgResult.Notes } else { @() }
            $result = $Rows | Export-PgResult -Path $dialog.FileName -Notes $notes -Description $Description
            Set-Status "Exported $('{0:N0}' -f $result.Rows) rows to $($result.Path) (with methodology sidecar)."
        } catch { Show-ErrorBox "Export failed: $($_.Exception.Message)" }
    }
}

$ui.PgDownloadButton.Add_Click({
    if ($script:Busy) { return }
    if (-not (Confirm-Box ("This one-time download is about 510 MB and needs roughly 1 GB " +
        "of free disk space while it unpacks. It can take several minutes on a slow connection." +
        "`n`nStart the download now?"))) { return }
    $ui.PgSummary.Text = 'Downloading... this tab will report when the dataset is ready.'
    Invoke-Async -Kind 'pg-download' -Params @{ PgModulePath = $script:PgModulePath } `
        -BusyMessage 'Downloading the CMS clinic-group reassignment dataset (~510 MB; this can take several minutes)...' `
        -WorkerScript 'param($PgModulePath) Import-Module $PgModulePath; Save-PgDataset' `
        -OnDone {
            param($result)
            Update-PgStatus
            $ui.PgSummary.Text = $result[0].Message + ' Enter a ZIP and click "Find practice groups".'
        } `
        -OnFail {
            param($message)
            Update-PgStatus
            $ui.PgSummary.Text = 'Download did not complete. Click "Download CMS dataset" to try again.'
            Show-ErrorBox $message
        }
})

$ui.PgRunButton.Add_Click({
    if ($script:Busy) { return }
    $zip = $ui.PgZipBox.Text.Trim()
    if ($zip -notmatch '^\d{3,5}\*?$' -or ($zip -match '^\d{1,4}$' -and $zip.Length -lt 5)) {
        Show-ErrorBox 'Enter a 5-digit ZIP code, or a prefix ending in * (e.g. 630*) for a wider area.'
        return
    }
    if (-not (Get-PgStatus).DatasetReady) {
        Show-ErrorBox "The dataset isn't downloaded yet — click 'Download CMS dataset' first (one-time, ~510 MB)."
        return
    }
    Invoke-Async -Kind 'pg-run' -Params @{ PgModulePath = $script:PgModulePath; Zip = $zip } `
        -BusyMessage "Finding practice groups for $zip — NPPES lookup, then matching against the reassignment roster (first run also loads ~510 MB into memory)..." `
        -WorkerScript 'param($PgModulePath, $Zip) Import-Module $PgModulePath; Get-PgGroupsInZip -Zip $Zip' `
        -OnDone {
            param($result)
            $pg = $result[0]
            $script:PgResult = $pg
            $script:PgFootprintSources = @{}   # cleared for the new ZIP
            $groups = @($pg.Groups)
            $rosters = @($pg.Rosters)
            $ui.PgGroupGrid.ItemsSource = (ConvertTo-DataTable -Rows $groups -Columns $script:PgGroupCols).DefaultView
            $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rosters -Columns $script:PgRosterCols).DefaultView
            $ui.PgRosterLabel.Text = 'Therapist roster — all groups (select a group above to filter):'
            $ui.PgExportGroupsButton.IsEnabled = ($groups.Count -gt 0)
            $ui.PgExportRosterButton.IsEnabled = ($rosters.Count -gt 0)
            # The footprint bridge needs the Referral map (shared-patient) dataset.
            $ui.PgFootprintButton.IsEnabled = ($groups.Count -gt 0 -and (Get-RmStatus).DatasetReady)
            $ui.PgSummary.Text = ("ZIP $($pg.Zip): $($pg.TherapistCount) individual therapists found; " +
                "$($groups.Count) multi-provider practice group(s) operate here; " +
                "$($pg.SoloCount) therapists are solo or not in a named group. " +
                'RosterSize is nationwide — a big roster with few in-ZIP members is a multi-site organization.')
            Set-Status "Practice groups for $($pg.Zip) complete."
        } `
        -OnFail {
            param($message)
            Update-PgStatus
            Show-ErrorBox $message
        }
})

$ui.PgGroupGrid.Add_SelectionChanged({
    if (-not $script:PgResult) { return }
    $row = $ui.PgGroupGrid.SelectedItem
    if ($row -is [System.Data.DataRowView]) {
        $pac = [string]$row.Row['GroupPacId']
        $name = [string]$row.Row['GroupName']
        $filtered = @($script:PgResult.Rosters | Where-Object { $_.GroupPacId -eq $pac })
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $filtered -Columns $script:PgRosterCols).DefaultView
        $label = "Roster for $name — $($filtered.Count) therapist(s)."
        # If a footprint was computed, show THIS group's top 2015 sources, keyed
        # by the unique PAC ID (group names are not unique).
        if ($script:PgFootprintSources.ContainsKey($pac)) {
            $tops = @($script:PgFootprintSources[$pac] | Select-Object -First 3 |
                ForEach-Object { "$($_.SourceName) ($($_.SharedPatients))" })
            if ($tops.Count) { $label += '  Top 2015 sources for local therapists: ' + ($tops -join ', ') + '.' }
        }
        $ui.PgRosterLabel.Text = $label
    } else {
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows @($script:PgResult.Rosters) -Columns $script:PgRosterCols).DefaultView
        $ui.PgRosterLabel.Text = 'Therapist roster — all groups (select a group above to filter):'
    }
})

$ui.PgFootprintButton.Add_Click({
    if ($script:Busy -or -not $script:PgResult) { return }
    # Roll each group's LOCAL (in-ZIP) therapists' 2015 inbound volume up to the
    # group. Key by GroupPacId (UNIQUE) — group names are not unique, and one
    # therapist can be in several groups, so the map value is a LIST of PAC IDs.
    $map = @{}
    foreach ($r in $script:PgResult.Rosters) {
        if ($r.InThisZip -eq 'Y') {
            $npi = [string]$r.NPI
            if (-not $map.ContainsKey($npi)) { $map[$npi] = New-Object System.Collections.Generic.List[string] }
            if (-not $map[$npi].Contains([string]$r.GroupPacId)) { $map[$npi].Add([string]$r.GroupPacId) }
        }
    }
    if ($map.Count -eq 0) { Show-ErrorBox 'No in-ZIP therapists to compute a footprint for.'; return }
    Invoke-Async -Kind 'pg-footprint' -Params @{ RmModulePath = $script:RmModulePath; Map = $map } `
        -BusyMessage 'Rolling 2015 referral volume up to each practice group (scanning ~35M shared-patient pairs)...' `
        -WorkerScript 'param($RmModulePath, $Map) Import-Module $RmModulePath; Get-RmInboundByBucket -TargetToBucket $Map -TopPerBucket 10' `
        -OnDone {
            param($result)
            $fp = @($result)
            $byPac = @{}
            foreach ($b in $fp) { $byPac[$b.Bucket] = $b; $script:PgFootprintSources[$b.Bucket] = @($b.TopSources) }
            # Augment each group row (matched by unique PAC ID) and rebind.
            $augmented = foreach ($g in @($script:PgResult.Groups)) {
                $val = if ($byPac.ContainsKey($g.GroupPacId)) { $byPac[$g.GroupPacId].SharedPatients } else { 0 }
                $g | Add-Member -NotePropertyName 'LocalReferrals2015' -NotePropertyValue $val -Force -PassThru
            }
            $script:PgResult.Groups = @($augmented)
            $ui.PgGroupGrid.ItemsSource = (ConvertTo-DataTable -Rows @($augmented) -Columns $script:PgGroupCols).DefaultView
            $withVol = @($fp | Where-Object { $_.SharedPatients -gt 0 }).Count
            $ui.PgSummary.Text = ("2015 referral footprint added: $withVol group(s) had local therapists with " +
                "shared-patient volume. Select a group to see its top sources. Reminder: 2015 vintage; a source " +
                'is a shared-patient proxy (labs/hospitals appear too — read by specialty).')
            Set-Status 'Referral footprint computed.'
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Footprint failed: $message"
        }
})

$ui.PgExportGroupsButton.Add_Click({
    if (-not $script:PgResult) { return }
    Export-PgWithDialog -Rows @($script:PgResult.Groups) `
        -SuggestedName "practice-groups-$($script:PgResult.Zip.TrimEnd('*')).csv" `
        -Description "Outpatient-rehab practice groups operating in ZIP $($script:PgResult.Zip) (CMS clinic-group reassignment data)"
})

$ui.PgExportRosterButton.Add_Click({
    if (-not $script:PgResult) { return }
    Export-PgWithDialog -Rows @($script:PgResult.Rosters) `
        -SuggestedName "practice-group-rosters-$($script:PgResult.Zip.TrimEnd('*')).csv" `
        -Description "Therapist rosters for practice groups in ZIP $($script:PgResult.Zip) (CMS clinic-group reassignment data)"
})

# ---------------------------------------------------------------------------
# Provider 360 lookup tab
# ---------------------------------------------------------------------------

$script:LkCols = @('NPI', 'Name', 'Specialty', 'SharedPatients', 'SameDay')

$ui.LkRunButton.Add_Click({
    if ($script:Busy) { return }
    $npi = $ui.LkNpiBox.Text.Trim()
    if ($npi -notmatch '^\d{10}$') { Show-ErrorBox 'Enter a full 10-digit NPI.'; return }

    # Eligibility comes from the in-memory O&R snapshot (main session); the
    # heavy NPPES + shared-patient + group work runs in a worker.
    $eligLine = 'Order & Referring eligibility: (data not loaded — use the Search tab''s "Check for updates" first)'
    if ($script:Data) {
        $chk = @($npi | Test-OrfNpi -Data $script:Data)[0]
        if ($chk.Status -like 'ELIGIBLE*') {
            $eligLine = "Order & Referring: ELIGIBLE — PartB=$($chk.PartB) DME=$($chk.DME) HHA=$($chk.HHA) PMD=$($chk.PMD) Hospice=$($chk.Hospice)"
        } elseif ($chk.Status -eq 'NOT ON LIST') {
            $eligLine = 'Order & Referring: NOT on the current eligible-to-order/refer list.'
        } else {
            $eligLine = "Order & Referring: $($chk.Status)."
        }
    }
    $script:LkEligLine = $eligLine
    $script:LkNpi = $npi
    $hasRm = (Get-RmStatus).DatasetReady
    $hasPg = (Get-PgStatus).DatasetReady

    $ui.LkDetail.Text = "Looking up $npi ..."
    $ui.LkExportInboundButton.IsEnabled = $false
    $ui.LkExportOutboundButton.IsEnabled = $false
    Invoke-Async -Kind 'lk-run' -Params @{
            RmModulePath = $script:RmModulePath; PgModulePath = $script:PgModulePath
            Npi = $npi; HasRm = $hasRm; HasPg = $hasPg
        } `
        -BusyMessage "Looking up $npi — NPPES, plus 2015 referral scan and practice groups if those datasets are present..." `
        -WorkerScript @'
param($RmModulePath, $PgModulePath, $Npi, $HasRm, $HasPg)
Import-Module $RmModulePath
$detail = (Get-RmProviderDetail -Npi @($Npi))[$Npi]
$activity = if ($HasRm) { Get-RmProviderReferralActivity -Npi $Npi } else { $null }
$groups = @()
if ($HasPg) { Import-Module $PgModulePath; $groups = @(Get-PgMembershipForNpi -Npi $Npi) }
[pscustomobject]@{ Detail = $detail; Activity = $activity; Groups = $groups; HasRm = $HasRm; HasPg = $HasPg }
'@ `
        -OnDone {
            param($result)
            $r = $result[0]
            $d = $r.Detail
            $lines = New-Object System.Collections.Generic.List[string]
            $nm = if ($d -and $d.Name) { $d.Name } else { '(name not found in NPPES)' }
            $loc = if ($d -and ($d.City -or $d.State)) { " — $($d.City), $($d.State)" } else { '' }
            $spec = if ($d -and $d.Specialty) { "  |  $($d.Specialty)" } else { '' }
            $lines.Add("NPI $($script:LkNpi):  $nm$loc$spec")
            $lines.Add($script:LkEligLine)
            if ($r.HasPg) {
                $gs = @($r.Groups)
                if ($gs.Count) {
                    $lines.Add("Practice groups: " + (@($gs | ForEach-Object { "$($_.GroupName) [$($_.State), roster $($_.RosterSize)]" }) -join '; '))
                } else { $lines.Add('Practice groups: none on record (solo, or not reassigning to a group).') }
            } else {
                $lines.Add('Practice groups: (Practice groups dataset not downloaded.)')
            }
            $ui.LkDetail.Text = ($lines -join "`n")

            if ($r.HasRm -and $r.Activity) {
                $inb = @($r.Activity.Inbound); $outb = @($r.Activity.Outbound)
                $script:LkInbound = $inb; $script:LkOutbound = $outb
                $ui.LkInboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $inb -Columns $script:LkCols).DefaultView
                $ui.LkOutboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $outb -Columns $script:LkCols).DefaultView
                $ui.LkInboundLabel.Text = "Referral sources (who shared patients INTO them, 2015) — $($inb.Count):"
                $ui.LkOutboundLabel.Text = "Referral destinations (who they shared patients ONWARD to, 2015) — $($outb.Count):"
                $ui.LkExportInboundButton.IsEnabled = ($inb.Count -gt 0)
                $ui.LkExportOutboundButton.IsEnabled = ($outb.Count -gt 0)
            } else {
                $script:LkInbound = @(); $script:LkOutbound = @()
                $ui.LkInboundGrid.ItemsSource = $null
                $ui.LkOutboundGrid.ItemsSource = $null
                $ui.LkInboundLabel.Text = 'Referral activity: (Referral map dataset not downloaded — download it on that tab to see 2015 referral sources/destinations.)'
                $ui.LkOutboundLabel.Text = ''
            }
            Set-Status "Lookup for $($script:LkNpi) complete."
        } `
        -OnFail {
            param($message)
            $ui.LkDetail.Text = "Lookup failed: $message"
            Show-ErrorBox $message
        }
})

$ui.LkExportInboundButton.Add_Click({
    if (-not $script:LkInbound -or @($script:LkInbound).Count -eq 0) { return }
    Export-RmWithDialog -Rows @($script:LkInbound) -SuggestedName "referrals-in-$($script:LkNpi).csv" `
        -Description "2015 referral sources sharing patients into NPI $($script:LkNpi) (CMS shared-patient data)" `
        -Notes $script:LkNotes
})

$ui.LkExportOutboundButton.Add_Click({
    if (-not $script:LkOutbound -or @($script:LkOutbound).Count -eq 0) { return }
    Export-RmWithDialog -Rows @($script:LkOutbound) -SuggestedName "referrals-out-$($script:LkNpi).csv" `
        -Description "2015 referral destinations NPI $($script:LkNpi) shared patients onward to (CMS shared-patient data)" `
        -Notes $script:LkNotes
})

# ---------------------------------------------------------------------------
# Watchlist tab
# ---------------------------------------------------------------------------

$script:WlCols = @('NPI', 'Status', 'ChangeSinceLast', 'LastName', 'FirstName',
                   'PartB', 'DME', 'HHA', 'PMD', 'Hospice')

$ui.WlSaveButton.Add_Click({
    if ($script:Busy) { return }
    $npis = @(Get-OrfNpiFromText -Text $ui.WlBox.Text)
    if ($npis.Count -eq 0) { Show-ErrorBox 'Paste at least one 10-digit NPI to save.'; return }
    try {
        $r = $npis | Set-OrfWatchlist
        $ui.WlBox.Text = (@(Get-OrfWatchlist) -join "`r`n")   # normalized view
        $ui.WlSummary.Text = "Saved $($r.Count) NPI(s) to your watchlist. Click 'Check now' to see their status."
    } catch { Show-ErrorBox "Could not save watchlist: $($_.Exception.Message)" }
})

$ui.WlCheckButton.Add_Click({
    if ($script:Busy) { return }
    if (-not $script:Data) {
        Show-ErrorBox "No data loaded yet. Click 'Check for updates' on the Search tab first."
        return
    }
    $npis = @(Get-OrfNpiFromText -Text $ui.WlBox.Text)
    if ($npis.Count -eq 0) { $npis = @(Get-OrfWatchlist) }
    if ($npis.Count -eq 0) { Show-ErrorBox 'Save or paste some NPIs first.'; return }
    Invoke-Async -Kind 'wl-check' -Params @{ ModulePath = $script:ModulePath; Npi = $npis } `
        -BusyMessage 'Checking your watchlist against the latest data and the previous update...' `
        -WorkerScript 'param($ModulePath, $Npi) Import-Module $ModulePath; Get-OrfWatchlistReport -Npi $Npi' `
        -OnDone {
            param($result)
            $rows = @($result)
            $script:WlReport = $rows
            $ui.WlGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows -Columns $script:WlCols).DefaultView
            $ui.WlExportButton.IsEnabled = ($rows.Count -gt 0)
            $dropped = @($rows | Where-Object { $_.ChangeSinceLast -eq 'Removed' }).Count
            $flag    = @($rows | Where-Object { $_.ChangeSinceLast -eq 'Changed' }).Count
            $notOn   = @($rows | Where-Object { $_.Status -eq 'NOT ON LIST' }).Count
            $ui.WlSummary.Text = ("$($rows.Count) watched provider(s): $notOn not on the current list" +
                $(if ($dropped) { ", $dropped dropped since the last update" } else { '' }) +
                $(if ($flag) { ", $flag had eligibility flags change" } else { '' }) +
                '. Sort by ChangeSinceLast to see what moved.')
            Set-Status 'Watchlist checked.'
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Watchlist check failed: $message"
        }
})

$ui.WlExportButton.Add_Click({
    if (-not $script:WlReport -or @($script:WlReport).Count -eq 0) { return }
    Export-WithDialog -Rows @($script:WlReport) -SuggestedName 'watchlist-report.csv' `
        -Description "Referrer watchlist status and change-since-last-update ($(@($script:WlReport).Count) NPIs)"
})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

# Load any saved watchlist into its box.
try { $ui.WlBox.Text = (@(Get-OrfWatchlist) -join "`r`n") } catch { }

# Sweep *.tmp download partials left by a previous run that was closed or killed
# mid-download. 2-minute floor so a scheduled-task download actively writing
# right now (recent LastWriteTime) is spared while abandoned partials are cleared.
try { Clear-OrfStaleTemp -OlderThanMinutes 2 } catch { }
try { Clear-RmStaleTemp -OlderThanMinutes 2 } catch { }
try { Clear-PgStaleTemp -OlderThanMinutes 2 } catch { }

Update-StatusFromDisk
Update-RmStatus
Update-PgStatus
if (Get-OrfLatestSnapshot) { Start-DataLoad }

# Safety net: any unhandled exception in a click/UI handler is shown in a
# message box and marked handled, so the app stays open instead of crashing out
# of ShowDialog (the failure mode a single non-technical user cannot recover
# from). Real errors are still surfaced — just not fatally.
try {
    $window.Dispatcher.add_UnhandledException({
        param($sender, $e)
        try {
            [void][System.Windows.MessageBox]::Show($window,
                "Something went wrong, but the app is still running:`n`n$($e.Exception.Message)",
                'Medicare Order & Referring Tracker',
                [System.Windows.MessageBoxButton]::OK, [System.Windows.MessageBoxImage]::Warning)
        } catch { }
        $e.Handled = $true
    })
} catch { }

# On close, stop the completion timer and best-effort tear down any in-flight
# background jobs so a partial download does not linger.
$window.Add_Closed({
    $timer.Stop()
    foreach ($job in @($script:Jobs)) {
        try { $job.PS.Stop(); $job.PS.Dispose(); $job.RS.Dispose() } catch { }
    }
    $script:Jobs.Clear()
})
[void]$window.ShowDialog()
