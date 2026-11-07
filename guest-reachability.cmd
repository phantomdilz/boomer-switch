@echo off
setlocal
set "BS_MARKER_FILE=%~f0"
powershell.exe -NoLogo -NoProfile -Command "$text=[IO.File]::ReadAllText($env:BS_MARKER_FILE); & ([scriptblock]::Create(($text -split ':POWERSHELL\r?\n',2)[1]))"
set "BS_MARKER_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %BS_MARKER_RESULT%
:POWERSHELL
$ErrorActionPreference = 'Stop'

function Select-MarkerAddress($Adapters) {
    $wireless = @($Adapters | Where-Object { $_.Up -and $_.Kind -eq 'Wireless80211' })
    if ($wireless.Count -ne 1) {
        throw 'Connect this computer to guest Wi-Fi. Exactly one connected Wi-Fi adapter is required.'
    }
    $addresses = @($wireless[0].Addresses | Where-Object {
        $bytes = $_.GetAddressBytes()
        $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and (
            $bytes[0] -eq 10 -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
            ($bytes[0] -eq 192 -and $bytes[1] -eq 168))
    })
    if ($addresses.Count -ne 1) {
        throw 'The Wi-Fi adapter needs exactly one private IPv4 address. No listener was started.'
    }
    if (@($Adapters | Where-Object { $_.Up -and $_.Kind -eq 'Ethernet' -and $_.Addresses.Count -gt 0 }).Count) {
        Write-Host 'Note: Ethernet is also connected. This check binds only to the selected Wi-Fi address.'
    }
    return $addresses[0]
}

function Start-Marker([Net.IPAddress]$Address, [int]$Port = 8769, [int]$Seconds = 120) {
    $listener = [Net.Sockets.TcpListener]::new($Address, $Port)
    try {
        $listener.Start(2)
        $portNumber = $listener.LocalEndpoint.Port
        Write-Host ''
        Write-Host "On your phone, join the same guest Wi-Fi and open:"
        Write-Host "http://${Address}:${portNumber}/"
        Write-Host "The check stops after $Seconds seconds. Press Ctrl+C to stop sooner."
        Write-Host 'This page only checks reachability. It has no files, sign-in, or light controls.'
        $body = [Text.Encoding]::UTF8.GetBytes("BOOMER SWITCH REACHABILITY ONLY`nNo authentication or device control.`n")
        $header = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: text/plain; charset=utf-8`r`nCache-Control: no-store`r`nConnection: close`r`nContent-Length: $($body.Length)`r`n`r`n")
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
            if (-not $listener.Pending()) { Start-Sleep -Milliseconds 50; continue }
            $client = $listener.AcceptTcpClient()
            try {
                $remaining = [Math]::Min(1000, [Math]::Max(1, ($Seconds - $clock.Elapsed.TotalSeconds) * 1000))
                $client.ReceiveTimeout = [int]$remaining
                $client.SendTimeout = [int]$remaining
                $stream = $client.GetStream()
                $buffer = New-Object byte[] 2048
                if ($stream.Read($buffer, 0, $buffer.Length) -gt 0 -and $clock.Elapsed.TotalSeconds -lt $Seconds) {
                    $stream.Write($header, 0, $header.Length)
                    $stream.Write($body, 0, $body.Length)
                }
            } catch [IO.IOException] {
                # A phone can close or leave a connection idle. The finite check continues.
            } finally { $client.Close() }
        }
        Write-Host 'The reachability check stopped. The listener is closed.'
    } finally { $listener.Stop() }
}

# RUN
try {
    $adapters = @([Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object {
        [pscustomobject]@{
            Kind = $_.NetworkInterfaceType.ToString()
            Up = ($_.OperationalStatus -eq [Net.NetworkInformation.OperationalStatus]::Up)
            Addresses = @($_.GetIPProperties().UnicastAddresses | ForEach-Object { $_.Address })
        }
    })
    $address = Select-MarkerAddress $adapters
    Start-Marker $address
    Write-Host 'If the phone did not open the page, report that result. No firewall settings were changed.'
    exit 0
} catch {
    Write-Host ''
    Write-Host 'The reachability check could not start or continue.'
    Write-Host $_.Exception.Message
    Write-Host 'No files were served and no network settings were changed.'
    Write-Host 'Keep this window open and report the message above.'
    exit 1
}
