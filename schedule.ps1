# Registers a daily Windows scheduled task that runs the nursing job search.
#   .\schedule.ps1              daily at 08:00
#   .\schedule.ps1 -At 18:30    pick another time
#   .\schedule.ps1 -Remove      delete the task
param([string]$At = "08:00", [switch]$Remove)

$name = "Nursing Job Finder"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Output "Removed '$name'."
    return
}

$action   = New-ScheduledTaskAction -Execute "$PSScriptRoot\run.bat" -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -Daily -At $At
# Run late if the PC was off/asleep at the scheduled time.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "Scheduled '$name' daily at $At."
