# MT5 Strategy Tester WPF tree inspector
# Walks the UIAutomation tree under MT5's main window and prints control properties.
# Use this BEFORE filling in TesterConfigure() in MT5Bridge.cs to discover
# AutomationIds for Expert / Symbol / Period / Date / Inputs / Start.
#
# Run:  powershell -ExecutionPolicy Bypass -File inspect_mt5.ps1
# Or:   powershell -ExecutionPolicy Bypass -File inspect_mt5.ps1 -MaxDepth 5 -Filter "Tester"

param(
    [int]$MaxDepth = 4,
    [string]$Filter = "",
    [switch]$ChartFilter   # if set, hide ChartFrame children (they're noisy)
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$ErrorActionPreference = 'Continue'

# Find MT5 process by name
$mt5Proc = Get-Process | Where-Object {
    $_.ProcessName -eq 'terminal64' -and $_.MainWindowHandle -ne 0
} | Select-Object -First 1

if (-not $mt5Proc) {
    Write-Error "MT5 terminal64.exe not running, or has no visible main window."
    exit 1
}

Write-Host "=== MT5 process found ==="
Write-Host "PID:    $($mt5Proc.Id)"
Write-Host "Title:  '$($mt5Proc.MainWindowTitle)'"
Write-Host "HWND:   $($mt5Proc.MainWindowHandle)"
Write-Host ""

$mt5 = [System.Windows.Automation.AutomationElement]::FromHandle($mt5Proc.MainWindowHandle)
if (-not $mt5) {
    Write-Error "Could not bind AutomationElement to MT5 window."
    exit 1
}

function Format-Element {
    param($el, $depth)
    $indent = "  " * $depth
    try {
        $c = $el.Current
        $type = ($c.ControlType.ProgrammaticName -replace 'ControlType\.', '')
        $name = $c.Name
        $class = $c.ClassName
        $id = $c.AutomationId
        $rect = $c.BoundingRectangle

        $line = "$indent[$type]"
        if ($name)  { $line += " name='$name'" }
        if ($id)    { $line += " id='$id'" }
        if ($class) { $line += " class='$class'" }
        if ($rect.Width -gt 0) {
            $line += " rect=($([int]$rect.X),$([int]$rect.Y),$([int]$rect.Width)x$([int]$rect.Height))"
        }
        return $line
    } catch {
        return "$indent[ERROR: $_]"
    }
}

function Walk {
    param($el, $depth, $maxDepth)
    if ($depth -gt $maxDepth) { return }

    $line = Format-Element -el $el -depth $depth

    # Apply filters
    $shouldPrint = $true
    if ($Filter) {
        $shouldPrint = ($line -like "*$Filter*")
    }
    if ($ChartFilter -and ($line -like "*ChartFrame*" -or $line -like "*Bars*")) {
        $shouldPrint = $false
    }

    if ($shouldPrint) { Write-Output $line }

    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    try {
        $child = $walker.GetFirstChild($el)
        while ($child -ne $null) {
            Walk -el $child -depth ($depth + 1) -maxDepth $maxDepth
            $child = $walker.GetNextSibling($child)
        }
    } catch {
        Write-Output "$('  ' * ($depth+1))[walker error: $_]"
    }
}

Write-Host "=== Tree walk (max depth $MaxDepth) ==="
if ($Filter) { Write-Host "Filter: '$Filter'" }
Write-Host ""

Walk -el $mt5 -depth 0 -maxDepth $MaxDepth

Write-Host ""
Write-Host "=== Done. ==="
Write-Host ""
Write-Host "Next step: identify the Strategy Tester panel in the output above"
Write-Host "(usually a Pane/Window with 'Tester' in name or class), then re-run with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File inspect_mt5.ps1 -MaxDepth 6 -Filter '<unique substring of ST panel>'"
