# Azure App Registration for Outlook Poller
# Requires Azure CLI: 'az login'

$appName = "PO_Extraction_Outlook"
$replyUrl = "http://localhost:8080"

# Check if az is in path, if not add it
if (Get-Command az -ErrorAction SilentlyContinue) {
    Write-Host "Azure CLI found in PATH."
}
else {
    $azPath = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
    if (Test-Path "$azPath\az.cmd") {
        Write-Host "Adding Azure CLI to PATH: $azPath"
        $env:Path += ";$azPath"
    }
    else {
        Write-Error "Azure CLI not found. Please install it or add to PATH."
        exit 1
    }
}

Write-Host "Creating App Registration: $appName..."
$app = az ad app create --display-name $appName --web-redirect-uris $replyUrl | ConvertFrom-Json

$appId = $app.appId
$objectId = $app.id

Write-Host "App ID: $appId"
Write-Host "Object ID: $objectId"

# Add Microsoft Graph API Permissions (Mail.ReadWrite)
# Graph API Resource ID: 00000003-0000-0000-c000-000000000000
# Mail.ReadWrite Permission ID (Delegated): 024d486e-b451-40bb-833d-3e66d98c5c73
Write-Host "Adding Graph API permissions (Mail.ReadWrite)..."
az ad app permission add --id $appId --api 00000003-0000-0000-c000-000000000000 --api-permissions 024d486e-b451-40bb-833d-3e66d98c5c73=Scope

# Grant Admin Consent (Optional - requires admin rights)
Write-Host "Attempting to grant admin consent..."
az ad app permission grant --id $appId --api 00000003-0000-0000-c000-000000000000

# Create Client Secret
Write-Host "Creating Client Secret..."
$secret = az ad app credential reset --id $appId --display-name "PollerSecret" --years 2 | ConvertFrom-Json
$clientSecret = $secret.password

# Get Tenant ID
$tenantId = az account show --query tenantId -o tsv

Write-Host ""
Write-Host "---------------------------------------------------"
Write-Host "SETUP COMPLETE!"
Write-Host "---------------------------------------------------"
Write-Host "Please update your .env or run these commands to set variables:"
Write-Host ""
Write-Host "`$env:MS_GRAPH_CLIENT_ID = `"$appId`""
Write-Host "`$env:MS_GRAPH_TENANT_ID = `"$tenantId`""
Write-Host "`$env:MS_GRAPH_CLIENT_SECRET = `"$clientSecret`""
Write-Host ""
Write-Host "NOTE: If 'Admin Consent' failed (due to permissions), please go to Azure Portal > App Registrations > $appName > API Permissions and click 'Grant admin consent'."
