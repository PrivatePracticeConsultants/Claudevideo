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
        Width="1010" Height="700" MinWidth="820" MinHeight="520"
        WindowStartupLocation="CenterScreen" Background="#EEF1F4"
        FontFamily="Segoe UI" FontSize="12.5">
  <Window.Resources>
    <!-- App-wide design system: flat accent buttons, quiet bordered inputs,
         zebra-striped grids with slate headers. Textbook WPF only. -->
    <Style TargetType="Button">
      <Setter Property="Background" Value="#2C5F8A"/>
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="BorderBrush" Value="#24506F"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="12,5"/>
      <Setter Property="MinHeight" Value="28"/>
      <Setter Property="SnapsToDevicePixels" Value="True"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="4">
              <ContentPresenter Margin="{TemplateBinding Padding}"
                                HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#3A75A8"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="bd" Property="Background" Value="#1F476B"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="bd" Property="Background" Value="#C9D1D8"/>
                <Setter TargetName="bd" Property="BorderBrush" Value="#BAC3CB"/>
                <Setter Property="Foreground" Value="#717B84"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="DataGrid">
      <Setter Property="Background" Value="White"/>
      <Setter Property="BorderBrush" Value="#D5DBE1"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="RowHeight" Value="26"/>
      <Setter Property="AlternatingRowBackground" Value="#F5F8FA"/>
      <Setter Property="HorizontalGridLinesBrush" Value="#E8ECEF"/>
      <Setter Property="VerticalGridLinesBrush" Value="#EDF0F3"/>
      <Setter Property="RowHeaderWidth" Value="0"/>
    </Style>
    <Style TargetType="DataGridColumnHeader">
      <Setter Property="Background" Value="#EDF1F5"/>
      <Setter Property="Foreground" Value="#1F3B57"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Padding" Value="8,5"/>
      <Setter Property="BorderBrush" Value="#D5DBE1"/>
      <Setter Property="BorderThickness" Value="0,0,1,1"/>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Padding" Value="10,6"/>
      <Setter Property="FontSize" Value="13"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="Padding" Value="4,2"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="BorderBrush" Value="#B9C2CA"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Padding" Value="6,3"/>
    </Style>
  </Window.Resources>
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

    <TabControl Grid.Row="1" x:Name="Tabs" Background="White" BorderBrush="#D5DBE1" Padding="6">

      <!-- ============ Search tab ============ -->
      <TabItem Header="Search providers">
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
          <Border Grid.Row="3" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="SearchSummary" Foreground="#26333E" TextWrapping="Wrap"
                     Text="Enter a name or NPI and click Search."/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Batch check tab ============ -->
      <TabItem Header="Batch NPI check">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
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
          <Border Grid.Row="3" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="BatchSummary" Foreground="#26333E" TextWrapping="Wrap" Text=""/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Changes tab ============ -->
      <TabItem Header="What changed">
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
          <Border Grid.Row="2" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="ChangesSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="Snapshots accumulate automatically each time CMS publishes an update. Two or more are needed to compare."/>
          </Border>
        </Grid>
      </TabItem>
      <!-- ============ Referral map tab ============ -->
      <TabItem x:Name="RmTab" Header="Referral map (2015)">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="5*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="6*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" x:Name="RmIntroText" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
              Text="Enter a ZIP code to see which providers historically fed the most Medicare patients into each outpatient rehab clinic in that area. Built from the newest public CMS shared-patient release (Jan–Sep 2015, 30-day window) joined with the live NPPES registry — it maps the structure of the referral market, not current volumes."/>
          <WrapPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="ZIP:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="RmZipBox" Width="100" Height="28" VerticalContentAlignment="Center"
                     MaxLength="6" ToolTip="5-digit ZIP, or a prefix like 630* for a wider area"/>
            <TextBlock Text="Radius:" VerticalAlignment="Center" Margin="10,0,4,0"/>
            <ComboBox x:Name="RmRadiusCombo" MinWidth="110" Height="28" SelectedIndex="0"
                      ToolTip="Sweep every ZIP whose center lies within this many straight-line miles of the ZIP you typed. Wide radii in metro areas sweep many ZIPs and take longer.">
              <ComboBoxItem Content="Exact ZIP"/>
              <ComboBoxItem Content="5 miles"/>
              <ComboBoxItem Content="10 miles"/>
              <ComboBoxItem Content="15 miles"/>
              <ComboBoxItem Content="20 miles"/>
              <ComboBoxItem Content="25 miles"/>
              <ComboBoxItem Content="30 miles"/>
              <ComboBoxItem Content="40 miles"/>
              <ComboBoxItem Content="50 miles"/>
            </ComboBox>
            <CheckBox x:Name="RmOrgOnly" Content="Clinics (organizations) only" VerticalAlignment="Center"
                      Margin="14,0,0,0" ToolTip="Unchecked: also includes individual PT/OT/SLP providers (solo practices bill under individual NPIs)"/>
            <Button x:Name="RmRunButton" Content="Map referral sources" Padding="14,5" Margin="14,0,0,0"/>
            <Button x:Name="RmDownloadButton" Content="Download CMS dataset" Padding="10,5" Margin="10,0,0,0"/>
            <Button x:Name="RmImportButton" Content="Import CareSet file..." Padding="10,5" Margin="8,0,0,0"
                    ToolTip="Import a DocGraph Hop Teaming dataset (.zip or .csv) obtained from CareSet Systems — years newer than the free 2015 CMS data."/>
            <TextBlock Text="Active data:" VerticalAlignment="Center" Margin="12,0,4,0"/>
            <ComboBox x:Name="RmDatasetCombo" MinWidth="210" Height="28" VerticalContentAlignment="Center"
                      ToolTip="Every dataset on disk (downloaded or imported). Switching is instant — nothing is re-downloaded."/>
            <TextBlock x:Name="RmDataStatus" VerticalAlignment="Center" Margin="12,0,0,0" Foreground="#666"/>
          </WrapPanel>
          <DataGrid Grid.Row="2" x:Name="RmClinicGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="RmSourceLabel" FontWeight="SemiBold" Foreground="#1F3B57" Text="Referral sources (select a clinic above to filter):"
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
          <Border Grid.Row="5" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="RmSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="One-time setup: click 'Download CMS dataset' (~356 MB download, ~1.7 GB on disk)."/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Practice benchmark tab ============ -->
      <TabItem Header="Practice benchmark">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="3*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="4*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="4*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
              Text="Search for a practice (or paste its NPI), pick it from the results, and benchmark it against every other outpatient rehab provider in its ZIP: its rank, its share of the region's referral volume, and the sources feeding its competitors but not it. Uses whichever dataset is active on the Referral map tab."/>
          <WrapPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="Practice name or NPI:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="BmNameBox" Width="220" Height="28" VerticalContentAlignment="Center"
                     ToolTip="Part of the practice/clinic name, a therapist's last name, or a full 10-digit NPI"/>
            <TextBlock Text="State:" VerticalAlignment="Center" Margin="10,0,4,0"/>
            <TextBox x:Name="BmStateBox" Width="46" Height="28" VerticalContentAlignment="Center" MaxLength="2"
                     CharacterCasing="Upper" ToolTip="Optional 2-letter state to narrow the search"/>
            <Button x:Name="BmSearchButton" Content="Search NPPES" Padding="12,5" Margin="10,0,0,0"/>
            <Button x:Name="BmRunButton" Content="Benchmark selected" Padding="12,5" Margin="14,0,0,0" IsEnabled="False"/>
            <CheckBox x:Name="BmWiderArea" Content="Wider area (3-digit ZIP prefix)" VerticalAlignment="Center"
                      Margin="12,0,0,0" ToolTip="Compare across the whole 3-digit ZIP region instead of the practice's exact ZIP"/>
          </WrapPanel>
          <DataGrid Grid.Row="2" x:Name="BmSearchGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="BmRegionLabel" FontWeight="SemiBold" Foreground="#1F3B57" Text="Region ranking (run a benchmark to fill):" VerticalAlignment="Center"/>
            <Button x:Name="BmExportRegionButton" Content="Export ranking..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="BmRegionGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="5" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="BmMissedLabel" FontWeight="SemiBold" Foreground="#1F3B57" Text="Missed sources (feeding competitors, not this practice):" VerticalAlignment="Center"/>
            <Button x:Name="BmExportMissedButton" Content="Export missed sources..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="6" x:Name="BmMissedGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <Border Grid.Row="7" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="BmSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="Needs a referral dataset (Referral map tab) and an internet connection for the NPPES search."/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Practice groups tab ============ -->
      <TabItem Header="Practice groups">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="5*"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="6*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
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
          <WrapPanel Grid.Row="3" Orientation="Horizontal" Margin="0,6,0,4">
            <TextBlock x:Name="PgRosterLabel" FontWeight="SemiBold" Foreground="#1F3B57" Text="Therapist roster (select a group above to filter):"
                       VerticalAlignment="Center"/>
            <Button x:Name="PgFootprintButton" Content="Add referral benchmark" Padding="10,4"
                    Margin="14,0,0,0" IsEnabled="False"
                    ToolTip="Roll each group's LOCAL therapists' shared-patient volume up to the group: rank, market share, every source feeding each group, and per-group missed sources (needs a dataset on the Referral map tab)."/>
            <TextBlock Text="Show:" VerticalAlignment="Center" Margin="12,0,4,0"/>
            <ComboBox x:Name="PgViewCombo" MinWidth="150" Height="26" SelectedIndex="0" IsEnabled="False"
                      ToolTip="What the lower table shows for the selected group">
              <ComboBoxItem Content="Therapist roster"/>
              <ComboBoxItem Content="Referral sources"/>
              <ComboBoxItem Content="Missed sources"/>
              <ComboBoxItem Content="Sent patients to (outbound)"/>
              <ComboBoxItem Content="Source specialty mix"/>
            </ComboBox>
            <Button x:Name="PgTrendButton" Content="Group trend..." Padding="10,4" Margin="8,0,0,0" IsEnabled="False"
                    ToolTip="Year-over-year referral totals for the SELECTED group's local therapists across every imported CareSet year. One full file scan per year - minutes per year."/>
            <Button x:Name="PgExportGroupsButton" Content="Export groups..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"/>
            <Button x:Name="PgExportRosterButton" Content="Export view..." Padding="10,4"
                    Margin="8,0,0,0" IsEnabled="False"
                    ToolTip="Exports whatever the lower table currently shows (roster, referral sources, or missed sources)."/>
          </WrapPanel>
          <DataGrid Grid.Row="4" x:Name="PgRosterGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <Border Grid.Row="5" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="PgSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="One-time setup: click 'Download CMS dataset' (~510 MB download). This tab needs no other data."/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Multi-site chains tab ============ -->
      <TabItem Header="Multi-site chains">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
              Text="Break a chain (Ivy Rehab, ATI, Select, Athletico...) down into its parts. BY NPI lists every organization NPI trading under that name with its registered address and measured volume. BY ADDRESS goes further: it uses the Care Compare roster to find the clinicians at each street address and sums THEIR referral volume, which is the only way to get per-location figures - an organization NPI carries no service address."/>
          <WrapPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="Organization name:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="ChNameBox" Width="220" Height="28" VerticalContentAlignment="Center"
                     ToolTip="Part of the chain's name, e.g. IVYREHAB or ATI PHYSICAL THERAPY"/>
            <TextBlock Text="State:" VerticalAlignment="Center" Margin="10,0,4,0"/>
            <TextBox x:Name="ChStateBox" Width="46" Height="28" VerticalContentAlignment="Center" MaxLength="2"
                     CharacterCasing="Upper" ToolTip="Optional 2-letter state to narrow to one region"/>
            <TextBlock Text="ZIP:" VerticalAlignment="Center" Margin="10,0,4,0"/>
            <TextBox x:Name="ChZipBox" Width="70" Height="28" VerticalContentAlignment="Center" MaxLength="5"
                     ToolTip="Optional 5-digit ZIP - by address only, to isolate one location"/>
            <Button x:Name="ChNpiButton" Content="Break down by NPI" Padding="12,5" Margin="14,0,0,0"
                    ToolTip="Every organization NPI under this name: registered address, measured referral volume, and whether that NPI covers several sites."/>
            <Button x:Name="ChAddrButton" Content="Break down by ADDRESS" Padding="12,5" Margin="8,0,0,0"
                    ToolTip="Per-location referral volume, built from the individual clinicians Care Compare lists at each street address. Needs the Care Compare file (Practice groups tab: Download CMS dataset)."/>
            <Button x:Name="ChExportButton" Content="Export..." Padding="10,4" Margin="10,0,0,0" IsEnabled="False"/>
          </WrapPanel>
          <TextBlock Grid.Row="2" x:Name="ChLabel" FontWeight="SemiBold" Foreground="#1F3B57" Margin="0,2,0,4"
                     TextWrapping="Wrap" Text="Enter a chain name and pick a breakdown."/>
          <DataGrid Grid.Row="3" x:Name="ChGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <Border Grid.Row="4" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="ChSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="Needs a referral dataset (Referral map tab). The by-address breakdown also needs the Care Compare clinician file from the Practice groups tab."/>
          </Border>
        </Grid>
      </TabItem>

      <!-- ============ Provider 360 lookup tab ============ -->
      <TabItem Header="Provider lookup">
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
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
              Text="Enter any NPI for a single-provider profile that pulls together every dataset in this app: current eligibility and specialty, practice-group memberships, and their historical referral activity (who sent them patients, and who they sent onward). Optional data is shown when downloaded on the other tabs."/>
          <WrapPanel Grid.Row="1" Orientation="Horizontal" Margin="0,0,0,6">
            <TextBlock Text="NPI:" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBox x:Name="LkNpiBox" Width="140" Height="28" VerticalContentAlignment="Center"
                     MaxLength="10" ToolTip="A full 10-digit NPI"/>
            <Button x:Name="LkRunButton" Content="Look up provider" Padding="14,5" Margin="14,0,0,0"/>
            <Button x:Name="LkTrendButton" Content="Referral trend (multi-year)..." Padding="10,5" Margin="10,0,0,0"
                    ToolTip="Year-over-year referral totals for this NPI across every imported CareSet Hop Teaming year. Each year is a full file scan, so this takes minutes per year."/>
            <Button x:Name="LkExportTrendButton" Content="Export trend..." Padding="10,4" Margin="8,0,0,0" IsEnabled="False"/>
            <Button x:Name="LkGeoButton" Content="Referral heat map..." Padding="10,5" Margin="10,0,0,0"
                    ToolTip="Where this NPI's inbound referrals come from: every source located by practice ZIP, aggregated into a density table and an interactive map you can open in your browser."/>
            <Button x:Name="LkSaveMapButton" Content="Save map (HTML)..." Padding="10,4" Margin="8,0,0,0" IsEnabled="False"/>
            <Button x:Name="LkAnalysisButton" Content="Source analysis..." Padding="10,5" Margin="10,0,0,0"
                    ToolTip="A client-ready deep dive on this NPI's referral sources: concentration metrics (HHI, top-5 dependence), specialty mix, distance profile, referral-lag profile on CareSet data, charts, and auto-written findings."/>
            <Button x:Name="LkSaveReportButton" Content="Save report (HTML)..." Padding="10,4" Margin="8,0,0,0" IsEnabled="False"/>
            <CheckBox x:Name="LkTrendCheck" Content="Include year-over-year" VerticalAlignment="Center" Margin="12,0,0,0"
                      ToolTip="Also measure performance across EVERY imported CareSet year: volume and source count per year, source retention (kept / new / lost), and the biggest gains and declines. Adds a full scan per year, so it takes several minutes longer."/>
          </WrapPanel>
          <Border Grid.Row="2" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="10" Margin="0,0,0,8">
            <TextBlock x:Name="LkDetail" TextWrapping="Wrap" Foreground="#222"
                       Text="Enter an NPI and click Look up provider."/>
          </Border>
          <StackPanel Grid.Row="3" Orientation="Horizontal" Margin="0,4,0,4">
            <TextBlock x:Name="LkInboundLabel" FontWeight="SemiBold" Foreground="#1F3B57" VerticalAlignment="Center"
                       Text="Referral sources (who shared patients INTO them):"/>
            <Button x:Name="LkExportInboundButton" Content="Export..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="4" x:Name="LkInboundGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
          <StackPanel Grid.Row="5" Orientation="Horizontal" Margin="0,4,0,4">
            <TextBlock x:Name="LkOutboundLabel" FontWeight="SemiBold" Foreground="#1F3B57" VerticalAlignment="Center"
                       Text="Referral destinations (who they shared patients ONWARD to):"/>
            <Button x:Name="LkExportOutboundButton" Content="Export..." Padding="10,4" Margin="12,0,0,0" IsEnabled="False"/>
          </StackPanel>
          <DataGrid Grid.Row="6" x:Name="LkOutboundGrid" IsReadOnly="True" AutoGenerateColumns="True"
                    CanUserAddRows="False" GridLinesVisibility="Horizontal"
                    HeadersVisibility="Column" EnableRowVirtualization="True"/>
        </Grid>
      </TabItem>

      <!-- ============ Watchlist tab ============ -->
      <TabItem Header="Watchlist">
        <Grid Margin="10">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
            <RowDefinition Height="Auto"/>
          </Grid.RowDefinitions>
          <TextBlock Grid.Row="0" TextWrapping="Wrap" Foreground="#4A5560" Margin="0,0,0,8"
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
          <Border Grid.Row="3" Background="White" BorderBrush="#D5DBE1" BorderThickness="1"
                  CornerRadius="4" Padding="9,7" Margin="0,8,0,0">
            <TextBlock x:Name="WlSummary" Foreground="#26333E" TextWrapping="Wrap"
              Text="Paste NPIs, click Save watchlist, then Check now. Needs the Order &amp; Referring data (Search tab) downloaded."/>
          </Border>
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
    'RmTab', 'RmIntroText', 'RmZipBox', 'RmRadiusCombo', 'RmOrgOnly', 'RmRunButton', 'RmDownloadButton',
    'RmImportButton', 'RmDatasetCombo', 'RmDataStatus',
    'RmClinicGrid', 'RmSourceLabel', 'RmExportClinicsButton', 'RmExportSourcesButton',
    'RmSourceGrid', 'RmSummary',
    'BmNameBox', 'BmStateBox', 'BmSearchButton', 'BmRunButton', 'BmWiderArea',
    'BmSearchGrid', 'BmRegionLabel', 'BmExportRegionButton', 'BmRegionGrid',
    'BmMissedLabel', 'BmExportMissedButton', 'BmMissedGrid', 'BmSummary',
    'PgZipBox', 'PgRunButton', 'PgDownloadButton', 'PgDataStatus', 'PgGroupGrid',
    'PgRosterLabel', 'PgFootprintButton', 'PgViewCombo', 'PgTrendButton', 'PgExportGroupsButton', 'PgExportRosterButton',
    'PgRosterGrid', 'PgSummary',
    'ChNameBox', 'ChStateBox', 'ChZipBox', 'ChNpiButton', 'ChAddrButton', 'ChExportButton',
    'ChLabel', 'ChGrid', 'ChSummary',
    'LkNpiBox', 'LkRunButton', 'LkTrendButton', 'LkExportTrendButton',
    'LkGeoButton', 'LkSaveMapButton', 'LkAnalysisButton', 'LkSaveReportButton', 'LkTrendCheck', 'LkDetail',
    'LkInboundLabel', 'LkExportInboundButton',
    'LkInboundGrid', 'LkOutboundLabel', 'LkExportOutboundButton', 'LkOutboundGrid',
    'RmExportMixButton',
    'WlBox', 'WlSaveButton', 'WlCheckButton', 'WlExportButton', 'WlGrid', 'WlSummary'
)) {
    $ui[$name] = $window.FindName($name)
    if (-not $ui[$name]) { throw "Internal error: UI element '$name' not found." }
}

# Professional number display in every grid: thousands separators and right
# alignment for numeric columns, applied as columns auto-generate. Purely
# cosmetic and wrapped accordingly — a formatting quirk must never break the
# data bind itself.
$script:RightAlignStyle = $null
try {
    $script:RightAlignStyle = New-Object System.Windows.Style([System.Windows.Controls.TextBlock])
    $script:RightAlignStyle.Setters.Add((New-Object System.Windows.Setter(
        [System.Windows.Controls.TextBlock]::TextAlignmentProperty, [System.Windows.TextAlignment]::Right)))
} catch { }
$script:NumberFormatHandler = {
    param($sender, $e)
    try {
        if ($e.Column -isnot [System.Windows.Controls.DataGridTextColumn]) { return }
        $t = $e.PropertyType
        $isInt = ($t -eq [int] -or $t -eq [long])
        $isDouble = ($t -eq [double])
        if (-not ($isInt -or $isDouble)) { return }
        if ($e.PropertyName -eq 'Year') { return }   # "2,022" would be silly
        $e.Column.Binding.StringFormat = if ($isDouble) { 'N1' } else { 'N0' }
        if ($script:RightAlignStyle) { $e.Column.ElementStyle = $script:RightAlignStyle }
    } catch { }
}
foreach ($gname in @('SearchGrid', 'BatchGrid', 'ChangesGrid', 'RmClinicGrid', 'RmSourceGrid',
                     'BmSearchGrid', 'BmRegionGrid', 'BmMissedGrid', 'PgGroupGrid', 'PgRosterGrid',
                     'LkInboundGrid', 'LkOutboundGrid', 'WlGrid')) {
    $ui[$gname].Add_AutoGeneratingColumn($script:NumberFormatHandler)
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
                     $ui.RmRunButton, $ui.RmDownloadButton, $ui.RmImportButton, $ui.RmDatasetCombo, $ui.RmRadiusCombo,
                     $ui.BmSearchButton,
                     $ui.PgRunButton, $ui.PgDownloadButton, $ui.PgFootprintButton,
                     $ui.ChNpiButton, $ui.ChAddrButton,
                     $ui.LkRunButton, $ui.LkTrendButton, $ui.LkGeoButton, $ui.LkAnalysisButton, $ui.WlCheckButton)) {
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
        if ($Rows.Count -gt 0) {
            $v0 = $Rows[0].$c
            if ($v0 -is [int] -or $v0 -is [long]) { $type = [int64] }
            elseif ($v0 -is [double]) { $type = [double] }   # e.g. AvgDayWait — must sort numerically
        }
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
                         'ReferralSources', 'SharedPatients', 'SameDay', 'Chain', 'MultiSiteNPI', 'ExistedInDataYear')
$script:RmSourceCols = @('SourceNPI', 'SourceName', 'SourceSpecialty', 'SourceCity', 'SourceState',
                         'ClinicNPI', 'ClinicName', 'SharedPatients', 'SharedEvents', 'SameDay')

# The column set differs by dataset source (CMS rows carry SameDay; Hop
# Teaming rows carry AvgDayWait), so derive the grid columns from the rows
# actually returned; the static lists above are only the empty-result fallback.
function Get-RmDisplayColumns([object[]]$Rows, [string[]]$Fallback) {
    if ($Rows -and $Rows.Count -gt 0) { @($Rows[0].PSObject.Properties.Name) } else { $Fallback }
}

# Vintage facts about the ACTIVE dataset, refreshed by Update-RmStatus and
# used everywhere the UI mentions the data year. Defaults match the free CMS
# dataset so text is sensible before anything is downloaded.
$script:RmYear = 2015
$script:RmDataLabel = 'CMS 2015 shared-patient data'
$script:RmRowsLabel = '~35M'

$script:RmComboUpdating = $false   # guard: repopulating the combo fires SelectionChanged

function Update-RmStatus {
    $status = Get-RmStatus
    $script:RmYear = [int]$status.Year
    $script:RmDataLabel = [string]$status.Label
    $script:RmRowsLabel = if ($status.RowCount -gt 0) { '~{0:N0}' -f $status.RowCount } else { '~35M' }
    $ui.RmTab.Header = "Referral map ($($status.Year))"
    $ui.PgFootprintButton.Content = "Add $($status.Year) referral benchmark"
    # Dataset switcher: one item per dataset on disk, active one selected.
    # Guarded so the programmatic rebuild can't trigger a switch of its own.
    $script:RmComboUpdating = $true
    try {
        $sets = @(Get-RmAvailableDatasets)
        $script:RmDatasetChoices = $sets
        $ui.RmDatasetCombo.Items.Clear()
        foreach ($s in $sets) { [void]$ui.RmDatasetCombo.Items.Add($s.Label) }
        $activeIdx = -1
        for ($i = 0; $i -lt $sets.Count; $i++) { if ($sets[$i].Active) { $activeIdx = $i } }
        $ui.RmDatasetCombo.SelectedIndex = $activeIdx
        $ui.RmDatasetCombo.IsEnabled = ($sets.Count -gt 1 -and -not $script:Busy)
    } finally { $script:RmComboUpdating = $false }
    if ($status.DatasetReady) {
        $ui.RmDataStatus.Text = if ($status.Source -eq 'hop-teaming') {
            'Active dataset: DocGraph Hop Teaming {0} (CareSet), {1:N0} pairs' -f $status.Year, $status.RowCount
        } else {
            'Active dataset: CMS {0}, {1}-day window' -f $status.Year, $status.Interval
        }
    } else {
        $ui.RmDataStatus.Text = 'No dataset yet — download the free CMS data or import a CareSet file'
    }
    $ui.RmIntroText.Text = if ($status.DatasetReady -and $status.Source -eq 'hop-teaming') {
        ("Enter a ZIP code to see which providers fed the most Medicare patients into each outpatient rehab " +
         "clinic in that area. Built from the DocGraph Hop Teaming $($status.Year) dataset (CareSet Systems; " +
         "full-year Medicare FFS Part A+B claims) joined with the live NPPES registry — it maps the structure " +
         "of the referral market in $($status.Year), not this year's volumes.")
    } else {
        ("Enter a ZIP code to see which providers historically fed the most Medicare patients into each " +
         "outpatient rehab clinic in that area. Built from the newest public CMS shared-patient release " +
         "(Jan–Sep 2015, 30-day window) joined with the live NPPES registry — it maps the structure of the " +
         "referral market, not current volumes. Newer years are available by importing a CareSet file.")
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

# Methodology notes for a Provider 360 referral-activity export, built for
# whichever dataset is active.
function Get-LkNotes {
    $s = Get-RmStatus
    if ($s.Source -eq 'hop-teaming') {
        @(
            "Source: DocGraph Hop Teaming $($s.Year), produced by CareSet Systems from Medicare FFS Part A+B claims (data 'DocGraph' from CareSet)."
            "$($s.Year) vintage (full calendar year) — market structure, not this year's volumes. Medicare Advantage, Medicaid, and commercial plans are not included."
            'SharedPatients = distinct Medicare FFS patients shared in sequence (a referral proxy, not billed referrals). AvgDayWait = average days between the two providers'' visits — short waits look like referrals. Labs/imaging/hospitals appear from co-occurring care — read by specialty.'
            'Inbound = the other provider was seen FIRST (they shared into this NPI). Outbound = this NPI was seen first. Direction is claims sequence, not a literal referral.'
        )
    } else {
        @(
            "Source: CMS Physician Shared Patient Patterns (FOIA), $($s.Year), $($s.Interval)-day interval."
            "$($s.Year) vintage — market structure, not current volumes."
            'SharedPatients = unique Medicare beneficiaries shared in the interval window (a referral proxy, not billed referrals). Labs/imaging/hospitals appear from co-occurring care — read by specialty.'
            'Inbound = the other provider was seen FIRST (they shared into this NPI). Outbound = this NPI was seen first. Same-day pairs are attributed by CMS to the lower NPI, so direction near same-day is approximate.'
        )
    }
}

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

$ui.RmImportButton.Add_Click({
    if ($script:Busy) { return }
    $dialog = New-Object Microsoft.Win32.OpenFileDialog
    $dialog.Filter = 'CareSet DocGraph file (*.zip;*.csv)|*.zip;*.csv|All files (*.*)|*.*'
    $dialog.Title = 'Pick the DocGraph Hop Teaming file you downloaded from CareSet'
    if (-not $dialog.ShowDialog($window)) { return }
    $file = $dialog.FileName
    if (-not (Confirm-Box ("Import '$([System.IO.Path]::GetFileName($file))'?`n`n" +
        "The Hop Teaming dataset is large: importing needs up to ~9 GB of free disk space " +
        "and can take several minutes. It will become the active referral dataset " +
        "(the Download button switches back to the free CMS 2015 data any time)."))) { return }
    $ui.RmSummary.Text = 'Importing... this tab will report when the dataset is ready.'
    Invoke-Async -Kind 'rm-import' -Params @{ RmModulePath = $script:RmModulePath; File = $file } `
        -BusyMessage 'Importing the CareSet Hop Teaming dataset (validating, unpacking, and counting rows)...' `
        -WorkerScript 'param($RmModulePath, $File) Import-Module $RmModulePath; Import-RmDataset -Path $File' `
        -OnDone {
            param($result)
            Update-RmStatus
            $ui.RmSummary.Text = $result[0].Message + ' Enter a ZIP and click "Map referral sources".'
        } `
        -OnFail {
            param($message)
            Update-RmStatus
            $ui.RmSummary.Text = 'Import did not complete. Nothing was changed.'
            Show-ErrorBox $message
        }
})

# Dataset switcher: activating a different on-disk dataset is instant (a
# metadata write — no re-download). Results already on screen came from the
# previous dataset, so clear them rather than let them masquerade as the new
# year's numbers.
$ui.RmDatasetCombo.Add_SelectionChanged({
    if ($script:RmComboUpdating -or $script:Busy) { return }
    $idx = $ui.RmDatasetCombo.SelectedIndex
    if ($idx -lt 0 -or -not $script:RmDatasetChoices -or $idx -ge @($script:RmDatasetChoices).Count) { return }
    $choice = @($script:RmDatasetChoices)[$idx]
    if ($choice.Active) { return }
    try {
        $r = Set-RmActiveDataset -Source $choice.Source -Year $choice.Year -Interval $(if ($choice.Interval) { $choice.Interval } else { 30 })
        $script:RmResult = $null
        $ui.RmClinicGrid.ItemsSource = $null
        $ui.RmSourceGrid.ItemsSource = $null
        $ui.RmExportClinicsButton.IsEnabled = $false
        $ui.RmExportSourcesButton.IsEnabled = $false
        $ui.RmExportMixButton.IsEnabled = $false
        Update-RmStatus
        $ui.RmSummary.Text = $r.Message + ' Run a new map to see this year''s numbers.'
        Set-Status $r.Message
    } catch {
        Update-RmStatus   # snap the combo back to reality
        Show-ErrorBox "Could not switch datasets: $($_.Exception.Message)"
    }
})

$script:RmRadiusValues = @(0, 5, 10, 15, 20, 25, 30, 40, 50)

$ui.RmRunButton.Add_Click({
    if ($script:Busy) { return }
    $zip = $ui.RmZipBox.Text.Trim()
    if ($zip -notmatch '^\d{3,5}\*?$' -or ($zip -match '^\d{1,4}$' -and $zip.Length -lt 5)) {
        Show-ErrorBox 'Enter a 5-digit ZIP code, or a prefix ending in * (e.g. 630*) for a wider area.'
        return
    }
    $idx = [Math]::Max(0, $ui.RmRadiusCombo.SelectedIndex)
    $radius = if ($idx -lt $script:RmRadiusValues.Count) { $script:RmRadiusValues[$idx] } else { 0 }
    if ($radius -gt 0 -and $zip -notmatch '^\d{5}$') {
        Show-ErrorBox 'A radius search needs a full 5-digit center ZIP. Prefixes like 630* only work with "Exact ZIP".'
        return
    }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox ("No referral dataset is available yet - click 'Download CMS dataset' " +
            "(free 2015 data, ~356 MB) or 'Import CareSet file' (newer licensed data) first.")
        return
    }
    $year = $script:RmYear
    $areaLabel = if ($radius -gt 0) { "$zip +$radius mi" } else { $zip }
    Invoke-Async -Kind 'rm-run' -Params @{
            RmModulePath = $script:RmModulePath
            Zip = $zip
            Radius = $radius
            OrgOnly = [bool]$ui.RmOrgOnly.IsChecked
        } `
        -BusyMessage "Mapping referral sources for $areaLabel — NPPES sweep$(if ($radius -gt 0) { ' of every ZIP in the radius (minutes for wide radii in metro areas)' }), then a scan of $script:RmRowsLabel provider pairs (this can take several minutes on the larger datasets)..." `
        -WorkerScript 'param($RmModulePath, $Zip, $Radius, $OrgOnly) Import-Module $RmModulePath; Get-RmReferralMap -Zip $Zip -RadiusMiles $Radius -OrganizationsOnly:$OrgOnly' `
        -OnDone {
            param($result)
            $map = $result[0]
            $script:RmResult = $map
            $clinics = @($map.Clinics)
            $sources = @($map.Sources)
            $ui.RmClinicGrid.ItemsSource = (ConvertTo-DataTable -Rows $clinics `
                -Columns (Get-RmDisplayColumns $clinics $script:RmClinicCols)).DefaultView
            $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows $sources `
                -Columns (Get-RmDisplayColumns $sources $script:RmSourceCols)).DefaultView
            $ui.RmSourceLabel.Text = 'Referral sources — all clinics (select a clinic above to filter):'
            $ui.RmExportClinicsButton.IsEnabled = ($clinics.Count -gt 0)
            $ui.RmExportSourcesButton.IsEnabled = ($sources.Count -gt 0)
            $ui.RmExportMixButton.IsEnabled = ($sources.Count -gt 0)
            $withVolume = @($clinics | Where-Object { $_.SharedPatients -gt 0 }).Count
            $tooNew = @($clinics | Where-Object { $_.ExistedInDataYear -like 'No*' }).Count
            $year = $script:RmYear
            $ui.RmSummary.Text = ("ZIP $($map.Zip): $($clinics.Count) rehab provider(s) found in NPPES; " +
                "$withVolume had inbound shared-patient volume in the $year data " +
                "($('{0:N0}' -f $sources.Count) source relationships)." +
                $(if ($tooNew -gt 0) { " $tooNew did not have an NPI yet in $year (their zeros mean 'did not exist', not 'no referrals')." } else { '' }) +
                $(if ($map.PSObject.Properties['CoverageNote'] -and $map.CoverageNote) { " " + $map.CoverageNote } else { '' }) +
                $(if (@($clinics | Where-Object { $_.PSObject.Properties['MultiSiteNPI'] -and $_.MultiSiteNPI }).Count) {
                    " NOTE: $(@($clinics | Where-Object { $_.PSObject.Properties['MultiSiteNPI'] -and $_.MultiSiteNPI }).Count) provider(s) bill under a MULTI-SITE NPI - their volume covers every location that NPI serves, not just this address (see the MultiSiteNPI column)." } else { '' }) +
                $(if (@($clinics | Where-Object { $_.PSObject.Properties['Chain'] -and $_.Chain }).Count) {
                    " CHAIN (*): $(@($clinics | Where-Object { $_.PSObject.Properties['Chain'] -and $_.Chain }).Count) of these are one clinic of a multi-site company (their organization name holds 4+ org NPIs) - break them apart on the Multi-site chains tab." } else { '' }) +
                $(if ($map.PSObject.Properties['SiblingGroups'] -and @($map.SiblingGroups).Count) {
                    $sg = @($map.SiblingGroups)[0]
                    " POSSIBLE SAME COMPANY: $($sg.Organizations) organizations here share the name word '$($sg.LeadWord)' " +
                    "($('{0:N0}' -f $sg.CombinedPatients) patients between them) - a practice that gives each clinic its own LLC shows up as several small rows. " +
                    "Check the exported methodology for the full list." } else { '' }) +
                " Reminder: $year vintage — market structure, not current volumes; pairs under 11 patients are excluded per CMS privacy policy. Tip: a prefix like 630* widens the area.")
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
        $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows $filtered `
            -Columns (Get-RmDisplayColumns $filtered $script:RmSourceCols)).DefaultView
        $ui.RmSourceLabel.Text = "Referral sources for $([string]$row.Row['Name']) ($npi) — $($filtered.Count) source(s):"
    } else {
        $all = @($script:RmResult.Sources)
        $ui.RmSourceGrid.ItemsSource = (ConvertTo-DataTable -Rows $all `
            -Columns (Get-RmDisplayColumns $all $script:RmSourceCols)).DefaultView
        $ui.RmSourceLabel.Text = 'Referral sources — all clinics (select a clinic above to filter):'
    }
})

$ui.RmExportClinicsButton.Add_Click({
    if (-not $script:RmResult) { return }
    Export-RmWithDialog -Rows @($script:RmResult.Clinics) `
        -SuggestedName "rehab-clinics-$($script:RmResult.Zip.TrimEnd('*')).csv" `
        -Description "Outpatient rehab providers in ZIP $($script:RmResult.Zip), ranked by inbound shared-patient volume ($script:RmDataLabel)"
})

$ui.RmExportSourcesButton.Add_Click({
    if (-not $script:RmResult) { return }
    Export-RmWithDialog -Rows @($script:RmResult.Sources) `
        -SuggestedName "referral-sources-$($script:RmResult.Zip.TrimEnd('*')).csv" `
        -Description "Referral sources feeding outpatient rehab providers in ZIP $($script:RmResult.Zip) ($script:RmDataLabel)"
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
        -Description "Referral-source specialty mix for $scope ($script:RmDataLabel)" -Notes $notes
})

# ---------------------------------------------------------------------------
# Practice benchmark tab
# ---------------------------------------------------------------------------

$script:BmSearchRows = @()
$script:BmResult = $null

# The benchmark button needs BOTH a target (grid selection or a pasted NPI)
# and not-busy; Set-Busy doesn't manage it, so keep its state here.
function Update-BmRunEnabled {
    $hasTarget = ($ui.BmSearchGrid.SelectedItem -is [System.Data.DataRowView]) -or
                 ($ui.BmNameBox.Text.Trim() -match '^\d{10}$')
    $ui.BmRunButton.IsEnabled = ($hasTarget -and -not $script:Busy)
}

$ui.BmSearchButton.Add_Click({
    if ($script:Busy) { return }
    $term = $ui.BmNameBox.Text.Trim()
    if ($term.Length -lt 2) {
        Show-ErrorBox 'Type at least two letters of the practice name (or a full 10-digit NPI).'
        return
    }
    $state = $ui.BmStateBox.Text.Trim()
    if ($state -and $state -notmatch '^[A-Za-z]{2}$') {
        Show-ErrorBox "State must be two letters (e.g. MO) — or leave it empty."
        return
    }
    Invoke-Async -Kind 'bm-search' -Params @{
            RmModulePath = $script:RmModulePath; Term = $term; State = $state
        } `
        -BusyMessage "Searching NPPES for '$term'..." `
        -WorkerScript 'param($RmModulePath, $Term, $State)
            Import-Module $RmModulePath
            if ($State) { @(Find-RmPractice -Name $Term -State $State) } else { @(Find-RmPractice -Name $Term) }' `
        -OnDone {
            param($result)
            $rows = @($result | Where-Object { $null -ne $_ })
            $script:BmSearchRows = $rows
            $ui.BmSearchGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
                -Columns @('NPI','Name','Type','Specialty','City','State','Zip')).DefaultView
            $ui.BmSummary.Text = if ($rows.Count -eq 0) {
                "NPPES found nothing for '$($ui.BmNameBox.Text.Trim())'. Try fewer letters, drop the state, or paste the NPI directly."
            } else {
                "$($rows.Count) match(es). Select the practice, then click 'Benchmark selected'. Solo practices are usually the owner's Individual NPI; chains and clinics are Organization NPIs."
            }
            Update-BmRunEnabled
            Set-Status 'NPPES search complete.'
        } `
        -OnFail { param($message) Show-ErrorBox $message }
})

$ui.BmSearchGrid.Add_SelectionChanged({ Update-BmRunEnabled })
$ui.BmNameBox.Add_TextChanged({ if (-not $script:Busy) { Update-BmRunEnabled } })

$ui.BmRunButton.Add_Click({
    if ($script:Busy) { return }
    $sel = $ui.BmSearchGrid.SelectedItem
    $npi = if ($sel -is [System.Data.DataRowView]) { [string]$sel.Row['NPI'] }
           elseif ($ui.BmNameBox.Text.Trim() -match '^\d{10}$') { $ui.BmNameBox.Text.Trim() }
           else { $null }
    if (-not $npi) { Show-ErrorBox 'Select a practice from the search results first (or paste its 10-digit NPI).'; return }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox ("No referral dataset is available yet - on the Referral map tab, click " +
            "'Download CMS dataset' (free 2015 data) or 'Import CareSet file' first.")
        return
    }
    $ui.BmRunButton.IsEnabled = $false
    Invoke-Async -Kind 'bm-run' -Params @{
            RmModulePath = $script:RmModulePath; Npi = $npi
            Wider = [bool]$ui.BmWiderArea.IsChecked
        } `
        -BusyMessage "Benchmarking $npi against its region — NPPES sweep, then a scan of $script:RmRowsLabel provider pairs (several minutes on the larger datasets)..." `
        -WorkerScript 'param($RmModulePath, $Npi, $Wider)
            Import-Module $RmModulePath
            Get-RmPracticeBenchmark -Npi $Npi -WiderArea:$Wider' `
        -OnDone {
            param($result)
            $bm = $result[0]
            $script:BmResult = $bm
            $clinics = @($bm.Clinics)
            $missed = @($bm.MissedSources)
            $clinicCols = Get-RmDisplayColumns $clinics @('You','NPI','Name','Type','City','State','Zip','ReferralSources','SharedPatients','ExistedInDataYear')
            $ui.BmRegionGrid.ItemsSource = (ConvertTo-DataTable -Rows $clinics -Columns $clinicCols).DefaultView
            $ui.BmMissedGrid.ItemsSource = (ConvertTo-DataTable -Rows $missed `
                -Columns (Get-RmDisplayColumns $missed @('SourceNPI','SourceName','SourceSpecialty','PatientsToCompetitors','CompetitorsFed'))).DefaultView
            $ui.BmRegionLabel.Text = "Region ranking for '$($bm.Zip)' — $($bm.OfTotal) provider(s), $($bm.Year) data:"
            $ui.BmMissedLabel.Text = "Missed sources — feeding competitors in '$($bm.Zip)' but not this practice ($($missed.Count)):"
            $ui.BmExportRegionButton.IsEnabled = ($clinics.Count -gt 0)
            $ui.BmExportMissedButton.IsEnabled = ($missed.Count -gt 0)
            $ui.BmSummary.Text = ("$($bm.Practice.Name) ($($bm.Npi)): rank $($bm.Rank) of $($bm.OfTotal) in '$($bm.Zip)' " +
                "with $('{0:N0}' -f $bm.InboundPatients) inbound shared patients from $($bm.ReferralSources) source(s) — " +
                "$($bm.MarketSharePct)% of the region's measured referral volume ($($bm.Year) data, $script:RmDataLabel). " +
                $(if ($missed.Count -gt 0) { "Top missed source: $(@($missed)[0].SourceName) ($(@($missed)[0].PatientsToCompetitors) patients to $(@($missed)[0].CompetitorsFed) competitor(s))." } else { 'No missed sources - every measured feeder in the region already shares patients with this practice.' }) +
                ' Remember: an org NPI and its therapists'' individual NPIs split volume - benchmark both.')
            Update-BmRunEnabled
            Set-Status "Benchmark for $($bm.Npi) complete."
        } `
        -OnFail {
            param($message)
            Update-BmRunEnabled
            Show-ErrorBox $message
        }
})

$ui.BmExportRegionButton.Add_Click({
    if (-not $script:BmResult) { return }
    Export-RmWithDialog -Rows @($script:BmResult.Clinics) `
        -SuggestedName "benchmark-region-$($script:BmResult.Npi).csv" `
        -Description "Region ranking around practice $($script:BmResult.Npi) in '$($script:BmResult.Zip)' ($script:RmDataLabel)" `
        -Notes @($script:BmResult.Notes)
})

$ui.BmExportMissedButton.Add_Click({
    if (-not $script:BmResult) { return }
    Export-RmWithDialog -Rows @($script:BmResult.MissedSources) `
        -SuggestedName "benchmark-missed-sources-$($script:BmResult.Npi).csv" `
        -Description "Sources feeding competitors of practice $($script:BmResult.Npi) in '$($script:BmResult.Zip)' but not the practice itself ($script:RmDataLabel)" `
        -Notes @($script:BmResult.Notes)
})

# ---------------------------------------------------------------------------
# Practice groups tab
# ---------------------------------------------------------------------------

# The footprint column is named for the ACTIVE referral dataset's year
# (LocalReferrals2015, LocalReferrals2022, ...) so exports stay self-describing.
function Get-PgGroupCols { @('Rank', 'GroupName', 'State', 'TherapistsInZip', 'RosterSize', "LocalReferrals$($script:RmYear)", 'SharePct', 'GroupPacId') }
$script:PgRosterCols = @('GroupName', 'GroupPacId', 'NPI', 'FirstName', 'LastName', 'Specialty', 'InThisZip')
$script:PgFootprintSources = @{}   # group PAC ID -> top-sources array (after footprint run)
$script:PgFootprintYear = $null    # data year the footprint was computed from

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
            $script:PgBench = $null
            $script:PgViewRows = @()
            $script:PgViewKind = 'roster'
            $ui.PgViewCombo.SelectedIndex = 0
            $ui.PgViewCombo.IsEnabled = $false
            $groups = @($pg.Groups)
            $rosters = @($pg.Rosters)
            $ui.PgGroupGrid.ItemsSource = (ConvertTo-DataTable -Rows $groups -Columns (Get-PgGroupCols)).DefaultView
            Update-PgBottomView   # binds the roster view and syncs export state
            $ui.PgExportGroupsButton.IsEnabled = ($groups.Count -gt 0)
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

# What the lower table shows: therapist roster, the selected group's referral
# sources, or its missed sources. One helper keeps the grid, label, and export
# state consistent no matter which control changed.
$script:PgBench = $null          # Get-RmGroupBenchmark result after the benchmark runs
$script:PgViewRows = @()         # rows currently shown below (whatever the view)
$script:PgViewKind = 'roster'

function Update-PgBottomView {
    if (-not $script:PgResult) { return }
    $sel = $ui.PgGroupGrid.SelectedItem
    $pac = $null; $name = $null
    if ($sel -is [System.Data.DataRowView]) {
        $pac = [string]$sel.Row['GroupPacId']
        $name = [string]$sel.Row['GroupName']
    }
    $view = $ui.PgViewCombo.SelectedIndex
    if ($view -gt 0 -and -not $script:PgBench) { $view = 0 }   # sources views need the benchmark
    $fpY = if ($script:PgFootprintYear) { $script:PgFootprintYear } else { $script:RmYear }

    if ($view -eq 1) {
        $script:PgViewKind = 'sources'
        $rows = if ($pac) { @($script:PgBench.Edges | Where-Object { $_.Bucket -eq $pac }) }
                else { @($script:PgBench.Edges) }
        $script:PgViewRows = $rows
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
            -Columns @('GroupName','SourceNPI','SourceName','SourceSpecialty','SharedPatients','MembersFed')).DefaultView
        $ui.PgRosterLabel.Text = if ($pac) { "Referral sources feeding $name ($fpY) — $($rows.Count):" }
                                 else { "Referral sources by group ($fpY) — select a group to filter ($($rows.Count) rows):" }
    } elseif ($view -eq 2) {
        $script:PgViewKind = 'missed'
        if (-not $pac) {
            $script:PgViewRows = @()
            $ui.PgRosterGrid.ItemsSource = $null
            $ui.PgRosterLabel.Text = 'Missed sources: select a group above first.'
        } else {
            $rows = @(Get-RmGroupMissedSources -Edges @($script:PgBench.Edges) -Bucket $pac)
            $script:PgViewRows = $rows
            $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
                -Columns @('SourceNPI','SourceName','SourceSpecialty','PatientsToOtherGroups','GroupsFed')).DefaultView
            $ui.PgRosterLabel.Text = "Missed sources — feeding OTHER groups but not $name ($fpY) — $($rows.Count):"
        }
    } elseif ($view -eq 3) {
        $script:PgViewKind = 'outbound'
        $rows = if ($pac) { @($script:PgBench.OutboundEdges | Where-Object { $_.Bucket -eq $pac }) }
                else { @($script:PgBench.OutboundEdges) }
        $script:PgViewRows = $rows
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
            -Columns @('GroupName','DestNPI','DestName','DestSpecialty','SharedPatients','MembersSending')).DefaultView
        $ui.PgRosterLabel.Text = if ($pac) { "Where $name sent patients onward ($fpY) — $($rows.Count) destination(s):" }
                                 else { "Outbound destinations by group ($fpY) — select a group to filter ($($rows.Count) rows):" }
    } elseif ($view -eq 4) {
        $script:PgViewKind = 'mix'
        $srcRows = if ($pac) { @($script:PgBench.Edges | Where-Object { $_.Bucket -eq $pac }) }
                   else { @($script:PgBench.Edges) }
        $rows = if ($srcRows.Count) { @(Get-RmSourceSpecialtyMix -Rows $srcRows) } else { @() }
        $script:PgViewRows = $rows
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
            -Columns @('Specialty','Sources','SharedPatients','PctOfVolume')).DefaultView
        $ui.PgRosterLabel.Text = if ($pac) { "Referral-source specialty mix for $name ($fpY):" }
                                 else { "Referral-source specialty mix — all groups combined ($fpY):" }
    } else {
        $script:PgViewKind = 'roster'
        $rows = if ($pac) { @($script:PgResult.Rosters | Where-Object { $_.GroupPacId -eq $pac }) }
                else { @($script:PgResult.Rosters) }
        $script:PgViewRows = $rows
        $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows -Columns $script:PgRosterCols).DefaultView
        $label = if ($pac) { "Roster for $name — $($rows.Count) therapist(s)." }
                 else { 'Therapist roster — all groups (select a group above to filter):' }
        if ($pac -and $script:PgFootprintSources.ContainsKey($pac)) {
            $tops = @($script:PgFootprintSources[$pac] | Select-Object -First 3 |
                ForEach-Object { "$($_.SourceName) ($($_.SharedPatients))" })
            if ($tops.Count) { $label += "  Top $fpY sources: " + ($tops -join ', ') + '.' }
        }
        $ui.PgRosterLabel.Text = $label
    }
    $ui.PgExportRosterButton.IsEnabled = (@($script:PgViewRows).Count -gt 0)
    # Group trend needs a selected group and 2+ imported CareSet years.
    $hopYears = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' })
    $ui.PgTrendButton.IsEnabled = ($null -ne $pac -and $hopYears.Count -ge 2 -and -not $script:Busy)
}

$ui.PgGroupGrid.Add_SelectionChanged({ Update-PgBottomView })
$ui.PgViewCombo.Add_SelectionChanged({ if ($script:PgResult) { Update-PgBottomView } })

$ui.PgFootprintButton.Add_Click({
    if ($script:Busy -or -not $script:PgResult) { return }
    # Roll each group's LOCAL (in-ZIP) therapists' inbound volume up to the
    # group. Key by GroupPacId (UNIQUE) — group names are not unique, and one
    # therapist can be in several groups, so the map value is a LIST of PAC IDs.
    $map = @{}
    $names = @{}
    foreach ($g in @($script:PgResult.Groups)) { $names[[string]$g.GroupPacId] = [string]$g.GroupName }
    foreach ($r in $script:PgResult.Rosters) {
        if ($r.InThisZip -eq 'Y') {
            $npi = [string]$r.NPI
            if (-not $map.ContainsKey($npi)) { $map[$npi] = New-Object System.Collections.Generic.List[string] }
            if (-not $map[$npi].Contains([string]$r.GroupPacId)) { $map[$npi].Add([string]$r.GroupPacId) }
        }
    }
    if ($map.Count -eq 0) { Show-ErrorBox 'No in-ZIP therapists to compute a benchmark for.'; return }
    $fpYear = $script:RmYear
    Invoke-Async -Kind 'pg-benchmark' -Params @{ RmModulePath = $script:RmModulePath; Map = $map; Names = $names } `
        -BusyMessage "Benchmarking the groups on $fpYear referral volume (scanning $script:RmRowsLabel shared-patient pairs, then naming the sources)..." `
        -WorkerScript 'param($RmModulePath, $Map, $Names) Import-Module $RmModulePath; Get-RmGroupBenchmark -TargetToBucket $Map -BucketNames $Names' `
        -OnDone {
            param($result)
            $bench = $result[0]
            $script:PgBench = $bench
            $script:PgFootprintYear = $fpYear
            $byPac = @{}
            foreach ($b in @($bench.Buckets)) { $byPac[$b.Bucket] = $b }
            # Top-3 label sources per group from the full edge list.
            $script:PgFootprintSources = @{}
            foreach ($b in @($bench.Buckets)) {
                $script:PgFootprintSources[$b.Bucket] = @($bench.Edges |
                    Where-Object { $_.Bucket -eq $b.Bucket } | Select-Object -First 3)
            }
            # Augment each group row (matched by unique PAC ID), then show the
            # LEADERBOARD: groups ranked by rolled-up referral volume.
            $augmented = foreach ($g in @($script:PgResult.Groups)) {
                $hit = if ($byPac.ContainsKey($g.GroupPacId)) { $byPac[$g.GroupPacId] } else { $null }
                $g | Add-Member -NotePropertyName 'Rank' -NotePropertyValue $(if ($hit) { $hit.Rank } else { $null }) -Force
                $g | Add-Member -NotePropertyName 'SharePct' -NotePropertyValue $(if ($hit) { $hit.SharePct } else { 0 }) -Force
                $g | Add-Member -NotePropertyName "LocalReferrals$fpYear" -NotePropertyValue $(if ($hit) { $hit.InboundPatients } else { 0 }) -Force -PassThru
            }
            $sorted = @($augmented | Sort-Object -Property @{Expression = "LocalReferrals$fpYear"; Descending = $true},
                                                           @{Expression = 'GroupName'; Descending = $false})
            $script:PgResult.Groups = $sorted
            $ui.PgGroupGrid.ItemsSource = (ConvertTo-DataTable -Rows $sorted -Columns (Get-PgGroupCols)).DefaultView
            $ui.PgViewCombo.IsEnabled = $true
            Update-PgBottomView
            $withVol = @($bench.Buckets | Where-Object { $_.InboundPatients -gt 0 }).Count
            $top = if (@($bench.Buckets).Count -gt 0) { @($bench.Buckets)[0] } else { $null }
            $ui.PgSummary.Text = ("$fpYear group benchmark: $withVol group(s) had measured referral volume." +
                $(if ($top -and $top.InboundPatients -gt 0) { " #1 is $($top.GroupName) with $('{0:N0}' -f $top.InboundPatients) patients ($($top.SharePct)% of measured group volume) from $($top.Sources) source(s)." } else { '' }) +
                " Use 'Show' to flip the lower table between each group's roster, its referral sources, and its missed sources. Reminder: $fpYear vintage; a source is a shared-patient proxy (labs/hospitals appear too — read by specialty).")
            Set-Status 'Group benchmark computed.'
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Group benchmark failed: $message"
        }
})

$script:PgTrendNotes = @()

$ui.PgTrendButton.Add_Click({
    if ($script:Busy -or -not $script:PgResult) { return }
    $sel = $ui.PgGroupGrid.SelectedItem
    if ($sel -isnot [System.Data.DataRowView]) { Show-ErrorBox 'Select a group in the top table first.'; return }
    $pac = [string]$sel.Row['GroupPacId']
    $name = [string]$sel.Row['GroupName']
    $members = @($script:PgResult.Rosters |
        Where-Object { $_.GroupPacId -eq $pac -and $_.InThisZip -eq 'Y' } |
        ForEach-Object { [string]$_.NPI } | Sort-Object -Unique)
    if ($members.Count -eq 0) { Show-ErrorBox "No in-ZIP therapists on record for $name."; return }
    $hopYears = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' })
    if ($hopYears.Count -lt 2) {
        Show-ErrorBox ("A group trend needs at least two imported CareSet Hop Teaming years " +
            "(you have $($hopYears.Count)). Import more years on the Referral map tab first.")
        return
    }
    if (-not (Confirm-Box ("Build a $(@($hopYears).Count)-year referral trend for $name " +
        "($($members.Count) local therapist(s))?`n`nEach year is a full scan of that year's file — " +
        "several minutes per year ($(@($hopYears | ForEach-Object Year) -join ', ')). " +
        "The result appears in the lower table and can be exported."))) { return }
    Invoke-Async -Kind 'pg-trend' -Params @{ RmModulePath = $script:RmModulePath; Members = $members; Name = $name } `
        -BusyMessage "Building the multi-year trend for $name (one full scan per year - several minutes per year)..." `
        -WorkerScript 'param($RmModulePath, $Members, $Name) Import-Module $RmModulePath; Get-RmGroupTrend -MemberNpi $Members -GroupName $Name' `
        -OnDone {
            param($result)
            $trend = $result[0]
            $rows = @($trend.Rows)
            $script:PgViewKind = 'grouptrend'
            $script:PgViewRows = $rows
            $script:PgTrendNotes = @($trend.Notes)
            $ui.PgRosterGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
                -Columns @('Year','InboundSources','InboundPatients','MembersWithVolume','TopSources')).DefaultView
            $ui.PgRosterLabel.Text = "Referral trend for $($trend.GroupName) by year (Hop Teaming $(@($trend.Years) -join ', ')):"
            $ui.PgExportRosterButton.IsEnabled = ($rows.Count -gt 0)
            $first = $rows[0]; $last = $rows[$rows.Count - 1]
            $dir = if ($last.InboundPatients -gt $first.InboundPatients) { 'grew' }
                   elseif ($last.InboundPatients -lt $first.InboundPatients) { 'shrank' } else { 'held steady' }
            Set-Status ("Group trend complete: inbound volume $dir from $($first.InboundPatients) ($($first.Year)) " +
                "to $($last.InboundPatients) ($($last.Year)). FFS-only data - Medicare Advantage shift also moves " +
                "these numbers. Change the 'Show' dropdown to return to the other views.")
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Group trend failed: $message"
        }
})

$ui.PgExportGroupsButton.Add_Click({
    if (-not $script:PgResult) { return }
    Export-PgWithDialog -Rows @($script:PgResult.Groups) `
        -SuggestedName "practice-groups-$($script:PgResult.Zip.TrimEnd('*')).csv" `
        -Description "Outpatient-rehab practice groups operating in ZIP $($script:PgResult.Zip) (CMS clinic-group reassignment data)"
})

$ui.PgExportRosterButton.Add_Click({
    if (-not $script:PgResult -or @($script:PgViewRows).Count -eq 0) { return }
    $zipTag = $script:PgResult.Zip.TrimEnd('*')
    switch ($script:PgViewKind) {
        'sources' {
            Export-RmWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "group-referral-sources-$zipTag.csv" `
                -Description "Referral sources feeding practice groups in ZIP $($script:PgResult.Zip), rolled up from member therapists ($script:RmDataLabel)" `
                -Notes @($script:PgBench.Notes)
        }
        'missed' {
            Export-RmWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "group-missed-sources-$zipTag.csv" `
                -Description "Sources feeding OTHER practice groups in ZIP $($script:PgResult.Zip) but not the selected group ($script:RmDataLabel)" `
                -Notes @($script:PgBench.Notes)
        }
        'outbound' {
            Export-RmWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "group-outbound-$zipTag.csv" `
                -Description "Where practice groups in ZIP $($script:PgResult.Zip) sent patients onward, rolled up from member therapists ($script:RmDataLabel)" `
                -Notes @($script:PgBench.Notes)
        }
        'mix' {
            Export-RmWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "group-specialty-mix-$zipTag.csv" `
                -Description "Referral-source specialty mix for practice groups in ZIP $($script:PgResult.Zip) ($script:RmDataLabel)" `
                -Notes (@($script:PgBench.Notes) + @('', 'Specialty mix: the group''s referral sources grouped by NPPES primary specialty; PctOfVolume is each specialty''s share of the group''s inbound shared-patient volume.'))
        }
        'grouptrend' {
            Export-RmWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "group-trend-$zipTag.csv" `
                -Description "Year-over-year referral trend for the selected practice group (DocGraph Hop Teaming, CareSet)" `
                -Notes @($script:PgTrendNotes)
        }
        default {
            Export-PgWithDialog -Rows @($script:PgViewRows) `
                -SuggestedName "practice-group-rosters-$zipTag.csv" `
                -Description "Therapist rosters for practice groups in ZIP $($script:PgResult.Zip) (CMS clinic-group reassignment data)"
        }
    }
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
    $ui.LkExportTrendButton.IsEnabled = $false   # inbound grid is about to be replaced
    $ui.LkSaveMapButton.IsEnabled = $false
    $ui.LkSaveReportButton.IsEnabled = $false
    Invoke-Async -Kind 'lk-run' -Params @{
            RmModulePath = $script:RmModulePath; PgModulePath = $script:PgModulePath
            Npi = $npi; HasRm = $hasRm; HasPg = $hasPg
        } `
        -BusyMessage "Looking up $npi — NPPES, plus referral scan and practice groups if those datasets are present..." `
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
                $ui.LkInboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $inb `
                    -Columns (Get-RmDisplayColumns $inb $script:LkCols)).DefaultView
                $ui.LkOutboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $outb `
                    -Columns (Get-RmDisplayColumns $outb $script:LkCols)).DefaultView
                $lkYear = if ($r.Activity.PSObject.Properties['Year']) { $r.Activity.Year } else { $script:RmYear }
                $ui.LkInboundLabel.Text = "Referral sources (who shared patients INTO them, $lkYear) — $($inb.Count):"
                $ui.LkOutboundLabel.Text = "Referral destinations (who they shared patients ONWARD to, $lkYear) — $($outb.Count):"
                $ui.LkExportInboundButton.IsEnabled = ($inb.Count -gt 0)
                $ui.LkExportOutboundButton.IsEnabled = ($outb.Count -gt 0)
            } else {
                $script:LkInbound = @(); $script:LkOutbound = @()
                $ui.LkInboundGrid.ItemsSource = $null
                $ui.LkOutboundGrid.ItemsSource = $null
                $ui.LkInboundLabel.Text = 'Referral activity: (no referral dataset yet — on the Referral map tab, download the CMS data or import a CareSet file.)'
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
        -Description "Referral sources sharing patients into NPI $($script:LkNpi) ($script:RmDataLabel)" `
        -Notes (Get-LkNotes)
})

$ui.LkExportOutboundButton.Add_Click({
    if (-not $script:LkOutbound -or @($script:LkOutbound).Count -eq 0) { return }
    Export-RmWithDialog -Rows @($script:LkOutbound) -SuggestedName "referrals-out-$($script:LkNpi).csv" `
        -Description "Referral destinations NPI $($script:LkNpi) shared patients onward to ($script:RmDataLabel)" `
        -Notes (Get-LkNotes)
})

# Multi-year referral trend: scans every imported Hop Teaming year for one
# NPI. Deliberately long-running (a full file scan per year), so it confirms
# first and states the cost. Results land in the inbound grid area.
$script:LkTrend = $null

$ui.LkTrendButton.Add_Click({
    if ($script:Busy) { return }
    $npi = $ui.LkNpiBox.Text.Trim()
    if ($npi -notmatch '^\d{10}$') { Show-ErrorBox 'Enter a full 10-digit NPI first.'; return }
    $hopYears = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' })
    if ($hopYears.Count -lt 2) {
        Show-ErrorBox ("A trend needs at least two imported CareSet Hop Teaming years " +
            "(you have $($hopYears.Count)). Import more years on the Referral map tab first.")
        return
    }
    if (-not (Confirm-Box ("Build a $(@($hopYears).Count)-year referral trend for NPI $npi ?`n`n" +
        "Each year is a full scan of that year's file, so this can take several minutes per year " +
        "($(@($hopYears | ForEach-Object Year) -join ', ')). The app stays responsive; " +
        "the result appears in the table below and can be exported."))) { return }
    $ui.LkSaveMapButton.IsEnabled = $false
    $ui.LkSaveReportButton.IsEnabled = $false
    Invoke-Async -Kind 'lk-trend' -Params @{ RmModulePath = $script:RmModulePath; Npi = $npi } `
        -BusyMessage "Building the multi-year referral trend for $npi (one full scan per year — several minutes per year)..." `
        -WorkerScript 'param($RmModulePath, $Npi) Import-Module $RmModulePath; Get-RmProviderTrend -Npi $Npi' `
        -OnDone {
            param($result)
            $trend = $result[0]
            $script:LkTrend = $trend
            $rows = @($trend.Rows)
            $ui.LkInboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
                -Columns @('Year','InboundSources','InboundPatients','OutboundTargets','OutboundPatients','TopSources')).DefaultView
            $ui.LkInboundLabel.Text = "Referral trend for $($trend.Npi) by year (Hop Teaming $(@($trend.Years) -join ', ')):"
            $ui.LkExportTrendButton.IsEnabled = ($rows.Count -gt 0)
            $first = $rows[0]; $last = $rows[$rows.Count - 1]
            $dir = if ($last.InboundPatients -gt $first.InboundPatients) { 'grew' }
                   elseif ($last.InboundPatients -lt $first.InboundPatients) { 'shrank' } else { 'held steady' }
            Set-Status ("Trend complete: inbound volume $dir from $($first.InboundPatients) ($($first.Year)) " +
                "to $($last.InboundPatients) ($($last.Year)). Remember: FFS-only data — Medicare Advantage shift " +
                "also moves these numbers. Re-run 'Look up provider' to restore the normal view.")
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Trend failed: $message"
        }
})

$ui.LkExportTrendButton.Add_Click({
    if (-not $script:LkTrend) { return }
    Export-RmWithDialog -Rows @($script:LkTrend.Rows) `
        -SuggestedName "referral-trend-$($script:LkTrend.Npi).csv" `
        -Description "Year-over-year referral trend for NPI $($script:LkTrend.Npi) (DocGraph Hop Teaming, CareSet)" `
        -Notes @($script:LkTrend.Notes)
})

# Referral heat map: all inbound pairs for one NPI, located by each source's
# NPPES practice ZIP, shown as a density table here and saved as an
# interactive HTML map. First run on a big practice is slow (one NPPES lookup
# per new source, cached forever after).
$script:LkGeo = $null

$ui.LkGeoButton.Add_Click({
    if ($script:Busy) { return }
    $npi = $ui.LkNpiBox.Text.Trim()
    if ($npi -notmatch '^\d{10}$') { Show-ErrorBox 'Enter a full 10-digit NPI first.'; return }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox ("No referral dataset is available yet - on the Referral map tab, click " +
            "'Download CMS dataset' (free 2015 data) or 'Import CareSet file' first.")
        return
    }
    $ui.LkSaveMapButton.IsEnabled = $false
    $ui.LkExportTrendButton.IsEnabled = $false
    $ui.LkSaveReportButton.IsEnabled = $false
    Invoke-Async -Kind 'lk-geo' -Params @{ RmModulePath = $script:RmModulePath; Npi = $npi } `
        -BusyMessage "Mapping where $npi's referrals come from — scanning $script:RmRowsLabel pairs, then locating each source in NPPES (first run on a big practice can take several minutes; lookups are cached)..." `
        -WorkerScript 'param($RmModulePath, $Npi) Import-Module $RmModulePath; Get-RmReferralGeography -Npi $Npi' `
        -OnDone {
            param($result)
            $geo = $result[0]
            $script:LkGeo = $geo
            $rows = @($geo.Rows)
            if ($rows.Count -eq 0) {
                $ui.LkInboundGrid.ItemsSource = $null
                $ui.LkInboundLabel.Text = "No measured inbound pairs for $($geo.Npi) in the $($geo.Year) data (pairs under 11 patients are excluded)."
                Set-Status 'Heat map: nothing to draw.'
                return
            }
            $ui.LkInboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows `
                -Columns @('Zip','City','State','Sources','SharedPatients','PctOfVolume','DistanceMiles','TopSource')).DefaultView
            $notMapped = $geo.TotalPatients - $geo.MappedPatients
            $ui.LkInboundLabel.Text = ("Referral density by source ZIP for $($geo.Practice.Name) ($($geo.Npi)), $($geo.Year) data — " +
                "$('{0:N0}' -f $geo.TotalPatients) patients across $($rows.Count) ZIP group(s)" +
                $(if ($notMapped -gt 0) { " ($('{0:N0}' -f $notMapped) not locatable)" }) + ':')
            $ui.LkSaveMapButton.IsEnabled = $true
            Set-Status "Heat map data ready - click 'Save map (HTML)...' to write and open the interactive map."
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Heat map failed: $message"
        }
})

$ui.LkSaveMapButton.Add_Click({
    if ($script:Busy -or -not $script:LkGeo) { return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'Interactive map (*.html)|*.html'
    $dialog.FileName = "referral-map-$($script:LkGeo.Npi).html"
    if (-not $dialog.ShowDialog($window)) { return }
    try {
        $r = Export-RmReferralMapHtml -Geography $script:LkGeo -Path $dialog.FileName
        # The density table + methodology sidecar ride along next to the map.
        $csvPath = [System.IO.Path]::ChangeExtension($dialog.FileName, '.csv')
        @($script:LkGeo.Rows) | Select-Object Zip, City, State, Sources, SharedPatients,
            PctOfVolume, DistanceMiles, TopSource |
            Export-RmResult -Path $csvPath -Notes @($script:LkGeo.Notes) `
                -Description "Referral density by source ZIP for NPI $($script:LkGeo.Npi) ($script:RmDataLabel)" | Out-Null
        Set-Status "Map saved: $($r.Path) ($($r.Points) ZIP circles) + density CSV + methodology."
        if (Confirm-Box "Map saved.`n`nOpen it in your browser now? (Drawing the background map needs an internet connection.)") {
            try { Start-Process $r.Path } catch { Show-ErrorBox "Could not open the browser: $($_.Exception.Message)" }
        }
    } catch {
        Show-ErrorBox "Saving the map failed: $($_.Exception.Message)"
    }
})

# ---------------------------------------------------------------------------
# Multi-site chains: an organization NPI carries no service address, so a
# chain's volume cannot be split by NPI alone. Two complementary views —
# BY NPI (what each legal entity draws) and BY ADDRESS (what each clinic
# draws, reconstructed from the clinicians Care Compare lists there).
# ---------------------------------------------------------------------------
$script:ChResult = $null
$script:ChKind = ''

function Get-ChSearchArgs {
    $name = $ui.ChNameBox.Text.Trim()
    if ($name.Length -lt 3) {
        Show-ErrorBox 'Enter at least 3 letters of the organization name, e.g. IVYREHAB.'
        return $null
    }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox ("No referral dataset is available yet - on the Referral map tab, click " +
            "'Download CMS dataset' (free 2015 data) or 'Import CareSet file' first.")
        return $null
    }
    $a = @{ Name = $name }
    $st = $ui.ChStateBox.Text.Trim()
    if ($st) {
        if ($st -notmatch '^[A-Za-z]{2}$') { Show-ErrorBox 'State must be two letters, e.g. NJ. Leave it blank for nationwide.'; return $null }
        $a['State'] = $st.ToUpperInvariant()
    }
    $a
}

$ui.ChNpiButton.Add_Click({
    if ($script:Busy) { return }
    $a = Get-ChSearchArgs
    if (-not $a) { return }
    $ui.ChExportButton.IsEnabled = $false
    Invoke-Async -Kind 'ch-npi' -Params @{ RmModulePath = $script:RmModulePath; SearchArgs = $a } `
        -BusyMessage ("Finding every NPI trading as '$($a.Name)' and measuring each one's referral volume - " +
            "one pass over $script:RmRowsLabel pairs, usually a few minutes.") `
        -WorkerScript 'param($RmModulePath, $SearchArgs) Import-Module $RmModulePath; Get-RmProviderFamily @SearchArgs' `
        -OnDone {
            param($result)
            $f = $result[0]
            $script:ChResult = $f; $script:ChKind = 'npi'
            $ui.ChGrid.ItemsSource = (ConvertTo-DataTable -Rows @($f.Rows) -Columns @(
                'NPI', 'Name', 'City', 'State', 'Zip', 'SharedPatients', 'ReferralSources',
                'PracticeSites', 'Chain', 'MultiSiteNPI')).DefaultView
            $ui.ChLabel.Text = ("BY NPI - '$($f.Search)' in $($f.Label): $('{0:N0}' -f $f.Npis) organization NPI(s), " +
                "$($f.NpisWithVolume) with measured volume, $('{0:N0}' -f $f.TotalPatients) patients in total.")
            $top = @($f.ByState | Select-Object -First 4 | ForEach-Object { "$($_.State) $($_.PctOfFamily)%" }) -join ', '
            $ui.ChSummary.Text = ("Volume by state: $top. " +
                "$($f.MultiSiteNpis) NPI(s) are flagged MULTI-SITE - their volume covers every clinic that NPI bills for and " +
                "CANNOT be split per location from this data. Use 'Break down by ADDRESS' for per-clinic figures.")
            $ui.ChExportButton.IsEnabled = $true
            Set-Status "Chain breakdown by NPI ready - $('{0:N0}' -f $f.TotalPatients) patients across $($f.Npis) NPI(s)."
        } `
        -OnFail { param($message) Show-ErrorBox "Chain breakdown failed: $message" }
})

$ui.ChAddrButton.Add_Click({
    if ($script:Busy) { return }
    $a = Get-ChSearchArgs
    if (-not $a) { return }
    $zip = $ui.ChZipBox.Text.Trim()
    if ($zip) {
        if ($zip -notmatch '^\d{5}$') { Show-ErrorBox 'ZIP must be 5 digits. Leave it blank for every location.'; return }
        $a['Zip'] = $zip
    }
    $ui.ChExportButton.IsEnabled = $false
    Invoke-Async -Kind 'ch-addr' -Params @{ RmModulePath = $script:RmModulePath; SearchArgs = $a } `
        -BusyMessage ("Building per-location referral figures for '$($a.Name)' from the Care Compare roster - " +
            "one pass over $script:RmRowsLabel pairs, usually a few minutes.") `
        -WorkerScript 'param($RmModulePath, $SearchArgs) Import-Module $RmModulePath; Get-RmLocationReferrals @SearchArgs' `
        -OnDone {
            param($result)
            $r = $result[0]
            $script:ChResult = $r; $script:ChKind = 'addr'
            $ui.ChGrid.ItemsSource = (ConvertTo-DataTable -Rows @($r.Rows) -Columns @(
                'Address', 'City', 'State', 'Zip', 'SharedPatients', 'ReferralSources',
                'ExclusivePatients', 'Clinicians', 'CliniciansWithVolume',
                'CliniciansAtOtherSites', 'SharedSitePatients')).DefaultView
            $ui.ChLabel.Text = ("BY ADDRESS - '$($r.Search)' in $($r.Label): $('{0:N0}' -f $r.Addresses) location(s), " +
                "$('{0:N0}' -f $r.AddressesWithVolume) with measured volume, " +
                "$('{0:N0}' -f $r.AttributedPatients) patients attributed across $('{0:N0}' -f $r.Clinicians) clinicians.")
            $overlapBit = if ($r.DoubleCountedPatients -gt 0) {
                (" The rows below add up to $('{0:N0}' -f $r.AddressRowTotal) because clinicians listed at MORE THAN ONE address are " +
                 "credited to each of their sites; the $('{0:N0}' -f $r.DoubleCountedPatients)-patient gap is that overlap (see CliniciansAtOtherSites / SharedSitePatients).")
            } else { '' }
            # A brand is usually enrolled under a different legal name, so
            # name the candidates rather than leaving a partial answer.
            $relBit = if (@($r.RelatedNames).Count) {
                " Related names in Care Compare that may be the same company - search them separately: " +
                (@($r.RelatedNames | ForEach-Object { "$($_.Name) ($($_.Addresses) locations)" }) -join '; ') + "."
            } else { ' Chains also register clinics under other legal names, so try a shorter fragment if a site you expect is missing.' }
            $ui.ChSummary.Text = ("This counts care billed under INDIVIDUAL clinician NPIs. A further " +
                "$('{0:N0}' -f $r.OrgNpiPatients) patients are billed under the organization's own NPI(s), which carry no " +
                "service address and are NOT spread across the sites - treat the two as separate views." + $overlapBit +
                " These are the locations enrolled under a name matching your search." + $relBit +
                " Export for the full table with methodology.")
            $ui.ChExportButton.IsEnabled = $true
            Set-Status "Per-location breakdown ready - $('{0:N0}' -f $r.AttributedPatients) patients across $($r.AddressesWithVolume) location(s)."
        } `
        -OnFail { param($message) Show-ErrorBox "Per-location breakdown failed: $message" }
})

$ui.ChExportButton.Add_Click({
    if (-not $script:ChResult) { return }
    $slug = ($script:ChResult.Search -replace '[^A-Za-z0-9]', '').ToLowerInvariant()
    if ($script:ChKind -eq 'addr') {
        Export-RmWithDialog -Rows @($script:ChResult.Rows) -Notes @($script:ChResult.Notes) `
            -SuggestedName "chain-by-address-$slug.csv" `
            -Description "Per-location referral volume for '$($script:ChResult.Search)' ($($script:ChResult.Label)), built from Care Compare practice addresses"
    } else {
        Export-RmWithDialog -Rows @($script:ChResult.Rows) -Notes @($script:ChResult.Notes) `
            -SuggestedName "chain-by-npi-$slug.csv" `
            -Description "Referral volume by organization NPI for '$($script:ChResult.Search)' ($($script:ChResult.Label))"
    }
})

# Source analysis: the client-ready deep dive on one org's referral base.
$script:LkAnalysis = $null

$ui.LkAnalysisButton.Add_Click({
    if ($script:Busy) { return }
    # Unlike the other lookup buttons, analysis accepts SEVERAL NPIs pasted
    # together (org + therapist NPIs) and combines them into one practice —
    # the fix for volume split across NPIs, which hits small practices hardest.
    $npis = @([regex]::Matches($ui.LkNpiBox.Text, '\d{10}') | ForEach-Object { $_.Value } | Select-Object -Unique)
    if ($npis.Count -eq 0) {
        Show-ErrorBox ('Enter a full 10-digit NPI first. Tip: paste SEVERAL NPIs (separated by ' +
            'spaces or commas) to analyze an org NPI plus its therapist NPIs as one combined practice.')
        return
    }
    if (-not (Get-RmStatus).DatasetReady) {
        Show-ErrorBox ("No referral dataset is available yet - on the Referral map tab, click " +
            "'Download CMS dataset' (free 2015 data) or 'Import CareSet file' first.")
        return
    }
    $ui.LkSaveReportButton.IsEnabled = $false
    $ui.LkSaveMapButton.IsEnabled = $false
    $ui.LkExportTrendButton.IsEnabled = $false
    # Year-over-year is opt-in: it adds a full scan PER imported year.
    $wantTrend = [bool]$ui.LkTrendCheck.IsChecked
    $hopYears = @(Get-RmAvailableDatasets | Where-Object { $_.Source -eq 'hop-teaming' })
    if ($wantTrend -and $hopYears.Count -lt 2) {
        Show-ErrorBox ("Year-over-year needs at least TWO imported CareSet years (found $($hopYears.Count)). " +
            "Import another year on the Referral map tab, or clear the checkbox.")
        return
    }
    $who = $npis[0] + $(if ($npis.Count -gt 1) { " (+$($npis.Count - 1) more, combined)" } else { '' })
    $trendBit = if ($wantTrend) { " Then repeating the scan across all $($hopYears.Count) imported years for the year-over-year section — several minutes per year." } else { '' }
    Invoke-Async -Kind 'lk-analysis' -Params @{ RmModulePath = $script:RmModulePath; Npi = $npis; WithTrend = $wantTrend } `
        -BusyMessage ("Analyzing $who's referral sources — scanning $script:RmRowsLabel pairs, naming and locating each source, then sweeping competitors within 10 miles (first run on a big practice can take several minutes; lookups are cached).$trendBit") `
        -WorkerScript 'param($RmModulePath, $Npi, $WithTrend) Import-Module $RmModulePath; $sa = Get-RmSourceAnalysis -Npi $Npi; if ($WithTrend) { $sa = Add-RmSourceTrend -Analysis $sa }; $sa' `
        -OnDone {
            param($result)
            $sa = $result[0]
            $script:LkAnalysis = $sa
            $rows = @($sa.Sources | Select-Object -First 25)
            if (@($sa.Sources).Count -eq 0) {
                $ui.LkInboundGrid.ItemsSource = $null
                $ui.LkInboundLabel.Text = "No measured inbound pairs for $($sa.Npi) in the $($sa.Year) data (pairs under 11 patients are excluded)."
                Set-Status 'Source analysis: nothing to analyze.'
                return
            }
            $cols = @('Rank','SourceNPI','SourceName','SourceSpecialty','City','State','SharedPatients','PctOfVolume','CumulativePct','DistanceMiles')
            if ($sa.IsHop) { $cols += 'AvgDayWait' }
            $ui.LkInboundGrid.ItemsSource = (ConvertTo-DataTable -Rows $rows -Columns $cols).DefaultView
            # Competitive is $null when the sweep failed (analysis still valid).
            $rankBit = if ($sa.PSObject.Properties['Competitive'] -and $sa.Competitive -and $sa.Competitive.Rank) {
                " Rank #$($sa.Competitive.Rank) of $('{0:N0}' -f $sa.Competitive.ProviderCount) rehab providers within $($sa.Competitive.RadiusMiles) mi."
            } else { '' }
            $combinedBit = if ($sa.NpiCount -gt 1) { " + $($sa.NpiCount - 1) affiliated NPI(s)" } else { '' }
            $trendBit2 = if ($sa.PSObject.Properties['Trend'] -and $sa.Trend -and @($sa.Trend.Years).Count -ge 2) {
                $t = $sa.Trend
                $d = if ($t.VolumeChangePct -gt 0) { 'up' } elseif ($t.VolumeChangePct -lt 0) { 'down' } else { 'flat' }
                " Year-over-year $($t.FirstYear)-$($t.LastYear): volume $d $([math]::Abs($t.VolumeChangePct))%."
            } else { '' }
            $ui.LkInboundLabel.Text = ("Source analysis for $($sa.Practice.Name) ($($sa.Npi)$combinedBit), $($sa.Year): " +
                "$('{0:N0}' -f $sa.TotalPatients) patients from $('{0:N0}' -f $sa.SourceCount) sources - " +
                "top-5 dependence $($sa.Top5Pct)%, concentration $($sa.Concentration) (HHI $('{0:N0}' -f $sa.HHI))." +
                $rankBit + $trendBit2 + " Top 25 sources shown:")
            $ui.LkSaveReportButton.IsEnabled = $true
            Set-Status "Source analysis ready - click 'Save report (HTML)...' for the full report with charts."
        } `
        -OnFail {
            param($message)
            Show-ErrorBox "Source analysis failed: $message"
        }
})

$ui.LkSaveReportButton.Add_Click({
    if ($script:Busy -or -not $script:LkAnalysis) { return }
    $dialog = New-Object Microsoft.Win32.SaveFileDialog
    $dialog.Filter = 'Analysis report (*.html)|*.html'
    $dialog.FileName = "source-analysis-$($script:LkAnalysis.Npi).html"
    if (-not $dialog.ShowDialog($window)) { return }
    try {
        $r = Export-RmSourceReportHtml -Analysis $script:LkAnalysis -Path $dialog.FileName
        $csvPath = [System.IO.Path]::ChangeExtension($dialog.FileName, '.csv')
        @($script:LkAnalysis.Sources) |
            Export-RmResult -Path $csvPath -Notes @($script:LkAnalysis.Notes) `
                -Description "Full ranked referral-source table for NPI $($script:LkAnalysis.Npi) ($script:RmDataLabel)" | Out-Null
        $extra = ''
        if ($script:LkAnalysis.PSObject.Properties['Trend'] -and $script:LkAnalysis.Trend) {
            $tPath = [System.IO.Path]::ChangeExtension($dialog.FileName, '.by-year.csv')
            @($script:LkAnalysis.Trend.Years) |
                Export-RmResult -Path $tPath -Notes @($script:LkAnalysis.Trend.Notes) `
                    -Description "Year-over-year referral performance for NPI $($script:LkAnalysis.Npi)" | Out-Null
            $mPath = [System.IO.Path]::ChangeExtension($dialog.FileName, '.movers.csv')
            @($script:LkAnalysis.Trend.Movers) |
                Export-RmResult -Path $mPath -Notes @($script:LkAnalysis.Trend.Notes) `
                    -Description "Per-source change, $($script:LkAnalysis.Trend.FirstYear) to $($script:LkAnalysis.Trend.LastYear), for NPI $($script:LkAnalysis.Npi)" | Out-Null
            $extra = ' + by-year CSV + movers CSV'
        }
        Set-Status "Report saved: $($r.Path) + full source CSV$extra + methodology."
        if (Confirm-Box ("Report saved.`n`nOpen it in your browser now? (Charts, map, and data are all built in - " +
                "only the map's street background needs an internet connection.)")) {
            try { Start-Process $r.Path } catch { Show-ErrorBox "Could not open the browser: $($_.Exception.Message)" }
        }
    } catch {
        Show-ErrorBox "Saving the report failed: $($_.Exception.Message)"
    }
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
