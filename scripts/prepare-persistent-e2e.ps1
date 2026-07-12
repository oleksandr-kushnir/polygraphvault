param(
    [string]$NextcloudBase = "http://127.0.0.1:18088",
    [string]$NextcloudUser = "admin",
    [Parameter(Mandatory = $true)][string]$NextcloudPassword,
    [string]$SyncerBase = "http://127.0.0.1:19630",
    [Parameter(Mandatory = $true)][string]$SyncerToken,
    [string]$SourceProject = "C:\Users\alexk\Documents\GitHub\multi-agents"
)

$ErrorActionPreference = "Stop"
$davRoot = "$($NextcloudBase.TrimEnd('/'))/remote.php/dav/files/$([uri]::EscapeDataString($NextcloudUser))"
$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${NextcloudUser}:${NextcloudPassword}"))
$davHeaders = @{ Authorization = "Basic $basic" }
$syncHeaders = @{ Authorization = "Bearer $SyncerToken" }

function ConvertTo-DavPath([string]$Path) {
    return (($Path -replace '\\', '/').Trim('/') -split '/' | ForEach-Object {
        [uri]::EscapeDataString($_)
    }) -join '/'
}

function Ensure-DavCollection([string]$Path) {
    $current = ""
    foreach ($part in (($Path -replace '\\', '/').Trim('/') -split '/')) {
        $current = if ($current) { "$current/$part" } else { $part }
        $url = "$davRoot/$(ConvertTo-DavPath $current)/"
        $request = [Net.HttpWebRequest]::Create($url)
        $request.Method = "MKCOL"
        $request.Headers[[Net.HttpRequestHeader]::Authorization] = $davHeaders.Authorization
        try {
            $response = $request.GetResponse()
            $response.Close()
        } catch {
            $errorResponse = $_.Exception.Response
            if (-not $errorResponse -and $_.Exception.InnerException) {
                $errorResponse = $_.Exception.InnerException.Response
            }
            $status = if ($errorResponse) { [int]$errorResponse.StatusCode } else { 0 }
            if ($status -ne 405) { throw }
        }
    }
}

function Put-DavBytes([string]$Path, [byte[]]$Bytes, [string]$ContentType) {
    $parent = ($Path -replace '\\', '/') -replace '/[^/]+$', ''
    Ensure-DavCollection $parent
    $url = "$davRoot/$(ConvertTo-DavPath $Path)"
    Invoke-WebRequest -Uri $url -Headers $davHeaders -Method Put -Body $Bytes -ContentType $ContentType -UseBasicParsing | Out-Null
}

function Put-DavFile([string]$Path, [string]$LocalPath, [string]$ContentType) {
    if (-not (Test-Path -LiteralPath $LocalPath)) { throw "Missing fixture: $LocalPath" }
    Put-DavBytes $Path ([IO.File]::ReadAllBytes($LocalPath)) $ContentType
}

function Put-DavText([string]$Path, [string]$Text) {
    Put-DavBytes $Path ([Text.UTF8Encoding]::new($false).GetBytes($Text)) "text/markdown; charset=utf-8"
}

function New-SpokenBriefing([string]$Text) {
    Add-Type -AssemblyName System.Speech
    $memory = New-Object IO.MemoryStream
    $voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $voice.SetOutputToWaveStream($memory)
        $voice.Speak($Text)
        return $memory.ToArray()
    } finally {
        $voice.Dispose()
        $memory.Dispose()
    }
}

function Ensure-Mapping([hashtable]$Definition) {
    $existing = @(Invoke-RestMethod -Uri "$SyncerBase/mappings" -Headers $syncHeaders)
    $match = $existing | Where-Object { $_.workspace_id -eq $Definition.workspace_id }
    if ($match) { return $match }
    return Invoke-RestMethod -Uri "$SyncerBase/mappings" -Headers $syncHeaders -Method Post -ContentType "application/json" -Body ($Definition | ConvertTo-Json)
}

$root = "PolyGraphRAG E2E"
$operations = "$root/Agent Operations Library"
$visual = "$root/Visual Security Library"
$audio = "$root/Audio Briefing Library"

Ensure-DavCollection $operations
Ensure-DavCollection $visual
Ensure-DavCollection $audio

Put-DavFile "$operations/guides/runtime-and-operations.md" "$SourceProject/docs/agent-docs/hermes/runtime-and-operations.md" "text/markdown"
Put-DavFile "$operations/research/inference-time-feedback.pdf" "$SourceProject/plan_to_implement/Inference-Time Feedback for Tool-Calling Agents.pdf" "application/pdf"
Put-DavText "$operations/lifecycle/update-check.md" @"
# Persistent lifecycle test

Version: 1

This document verifies that a changed Nextcloud file replaces its prior PolyGraphRAG document
without removing the last good graph copy if replacement ingestion fails.
"@

Put-DavFile "$visual/diagrams/nemoclaw-security-controls.jpg" "$SourceProject/plan_to_implement/archive/nemoclaw-security/1.jpg" "image/jpeg"
Put-DavText "$visual/notes/security-context.md" @"
# Visual security research context

The accompanying diagram compares filesystem isolation, network policy, privacy routing, and audit
trails. It is retained as a real multimodal fixture for vision extraction and graph queries.
"@

$briefing = @"
This is the persistent Poly Graph RAG audio briefing. Nextcloud is the source of truth.
The synchronizer recursively walks each mapped folder and sends supported files to one isolated
Poly Graph RAG workspace. Runtime mappings live in Postgres and are managed through the protected
mapping API. A healthy canary, bulk deletion threshold, and grace period protect graph documents.
"@
Put-DavBytes "$audio/briefings/sync-architecture-briefing.wav" (New-SpokenBriefing $briefing) "audio/wav"
Put-DavText "$audio/transcripts/sync-architecture-briefing.md" "# Audio briefing transcript`n`n$briefing"

$mappings = @(
    Ensure-Mapping @{
        nextcloud_path = $operations
        workspace_id = "agent_operations_library"
        workspace_name = "Agent Operations Library"
        create_workspace = $true
        path_root = "/nextcloud/$NextcloudUser"
        include_extensions = "md,pdf"
        min_files = 1
        max_delete_fraction = 0.75
    }
    Ensure-Mapping @{
        nextcloud_path = $visual
        workspace_id = "visual_security_library"
        workspace_name = "Visual Security Library"
        create_workspace = $true
        path_root = "/nextcloud/$NextcloudUser"
        include_extensions = "md,jpg"
        min_files = 1
        max_delete_fraction = 1.0
    }
    Ensure-Mapping @{
        nextcloud_path = $audio
        workspace_id = "audio_briefing_library"
        workspace_name = "Audio Briefing Library"
        create_workspace = $true
        path_root = "/nextcloud/$NextcloudUser"
        include_extensions = "md,wav"
        min_files = 1
        max_delete_fraction = 1.0
    }
)

$mappings | Select-Object id,nextcloud_path,workspace_id,enabled,include_extensions,min_files,max_delete_fraction | ConvertTo-Json -Depth 5
