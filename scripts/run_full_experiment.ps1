param(
    [string]$Python = "python",
    [ValidateSet("rule", "llm")][string]$Planner = "rule",
    [ValidateSet("ioa", "offline")][string]$Backend = "ioa",
    [int]$Repetitions = 1,
    [int]$Seed = 0,
    [string]$Output = "runs/full_experiment"
)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    & $Python -m co_ioa.experiments --backend $Backend --planner $Planner --repetitions $Repetitions --seed $Seed --output $Output
    if ($LASTEXITCODE -ne 0) { throw "Experiment process failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
