# Validate that all Strategy Tester control IDs are reachable and readable
# via UIAutomation. Run BEFORE building the DLL — proves the approach works.
#
# Run:  powershell -ExecutionPolicy Bypass -File validate_ids.ps1

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$ErrorActionPreference = 'Continue'

# Find MT5
$mt5Proc = Get-Process | Where-Object {
    $_.ProcessName -eq 'terminal64' -and $_.MainWindowHandle -ne 0
} | Select-Object -First 1

if (-not $mt5Proc) {
    Write-Error "MT5 not running."
    exit 1
}

$mt5 = [System.Windows.Automation.AutomationElement]::FromHandle($mt5Proc.MainWindowHandle)
Write-Host "MT5: '$($mt5Proc.MainWindowTitle)'" -ForegroundColor Cyan
Write-Host ""

# Find Strategy Tester inner dialog (id=10476)
$stInnerCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, "10476")
$stInner = $mt5.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $stInnerCond)

if (-not $stInner) {
    Write-Error "Strategy Tester inner dialog (id=10476) not found. Is the ST panel visible?"
    exit 1
}

Write-Host "Strategy Tester inner dialog found." -ForegroundColor Green
Write-Host ""

# Field map
$fields = @(
    @{ Name = 'Expert';        Id = '10485'; Type = 'ComboBox' }
    @{ Name = 'Symbol';        Id = '10486'; Type = 'ComboBox' }
    @{ Name = 'Timeframe';     Id = '10487'; Type = 'ComboBox' }
    @{ Name = 'Date type';     Id = '10123'; Type = 'ComboBox' }
    @{ Name = 'Start date';    Id = '10550'; Type = 'SysDateTimePick32' }
    @{ Name = 'End date';      Id = '10551'; Type = 'SysDateTimePick32' }
    @{ Name = 'Forward type';  Id = '10492'; Type = 'ComboBox' }
    @{ Name = 'Forward date';  Id = '10505'; Type = 'SysDateTimePick32' }
    @{ Name = 'Delays';        Id = '10488'; Type = 'ComboBox' }
    @{ Name = 'Modelling';     Id = '10515'; Type = 'ComboBox' }
    @{ Name = 'Profit pips';   Id = '11003'; Type = 'Button' }
    @{ Name = 'Deposit';       Id = '10489'; Type = 'ComboBox' }
    @{ Name = 'Currency';      Id = '10559'; Type = 'ComboBox' }
    @{ Name = 'Leverage';      Id = '10473'; Type = 'ComboBox' }
    @{ Name = 'Visual mode';   Id = '10490'; Type = 'Button' }
    @{ Name = 'Optimization';  Id = '10491'; Type = 'ComboBox' }
)

$found = 0
$missing = 0

Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f "Field", "Id", "Type", "Current Value") -ForegroundColor Yellow
Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f ("-" * 18), ("-" * 8), ("-" * 22), ("-" * 30)) -ForegroundColor Yellow

foreach ($f in $fields) {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $f.Id)
    $el = $stInner.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)

    if ($el) {
        $value = $el.Current.Name
        Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f $f.Name, $f.Id, $f.Type, $value) -ForegroundColor Green
        $found++
    } else {
        Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f $f.Name, $f.Id, $f.Type, "[NOT FOUND]") -ForegroundColor Red
        $missing++
    }
}

# Find Start button (in SysTabControl32, not the inner dialog)
$startCond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, "16790")
$startBtn = $mt5.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $startCond)

if ($startBtn) {
    Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f "Start", "16790", "Button", $startBtn.Current.Name) -ForegroundColor Green
    $found++
} else {
    Write-Host ("{0,-18} {1,-8} {2,-22} {3}" -f "Start", "16790", "Button", "[NOT FOUND]") -ForegroundColor Red
    $missing++
}

Write-Host ""
Write-Host "Result: $found found, $missing missing" -ForegroundColor Cyan
if ($missing -eq 0) {
    Write-Host ""
    Write-Host "All fields accessible. Safe to wire MT5Bridge.cs." -ForegroundColor Green
}
