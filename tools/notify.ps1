# Show a Windows toast notification (backlog B23). Windows PowerShell 5.1 WinRT classes only: no module, no network.
#   .\tools\notify.ps1 -Title "NHL DFS" -Message "test"
# The toast stays on screen until dismissed (reminder scenario). It shows only in the logged-on user's session,
# which is why the scheduled task runs as Ben with an interactive logon.
param([string]$Title = "NHL DFS", [string]$Message = "")
$ErrorActionPreference = "Stop"
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$t = [System.Security.SecurityElement]::Escape($Title)
$m = [System.Security.SecurityElement]::Escape($Message)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast scenario=`"reminder`"><visual><binding template=`"ToastGeneric`"><text>$t</text><text>$m</text></binding></visual><actions><action content=`"Dismiss`" arguments=`"dismiss`" activationType=`"system`"/></actions></toast>")
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
