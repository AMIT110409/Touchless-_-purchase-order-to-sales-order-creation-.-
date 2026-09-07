# setup_celonis_blob_container.ps1
# ---------------------------------
# Creates the 'celonis-tables' blob container in the existing
# Azure Storage Account (poextstorage49245) for holding Celonis reference tables.
#
# Prerequisites:
#   - Azure CLI installed and on PATH
#   - Logged in: az login
#   - Storage account 'poextstorage49245' already exists
#
# Usage:
#   cd "c:\Users\Abcom\Downloads\extraction ocr scripit\extraction ocr scripit"
#   powershell -ExecutionPolicy Bypass -File .\setup_celonis_blob_container.ps1

$storageAccountName = "poextstorage49245"
$resourceGroup      = "RG-WE-PROD-TouchlessOrderCapture-01"
$containerName      = "celonis-tables"

Write-Host ""
Write-Host "=================================================="
Write-Host "  Celonis Tables - Azure Blob Container Setup"
Write-Host "=================================================="
Write-Host ""
Write-Host "Storage Account : $storageAccountName"
Write-Host "Resource Group  : $resourceGroup"
Write-Host "Container       : $containerName"
Write-Host ""

# ── Step 1: Verify az CLI is available ────────────────────────────────────────
Write-Host "[1] Checking Azure CLI..."
$azVersion = az version --output tsv 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Azure CLI not found. Install from https://aka.ms/installazurecli"
    exit 1
}
Write-Host "    Azure CLI found."

# ── Step 2: Verify storage account exists ─────────────────────────────────────
Write-Host ""
Write-Host "[2] Verifying storage account '$storageAccountName' exists..."
$accountExists = az storage account show --name $storageAccountName --resource-group $resourceGroup --query "name" --output tsv 2>$null
if (-not $accountExists) {
    Write-Host "ERROR: Storage account '$storageAccountName' not found in '$resourceGroup'."
    Write-Host "       Please run setup_azure_storage.ps1 first."
    exit 1
}
Write-Host "    Found: $accountExists"

# ── Step 3: Get storage account key ───────────────────────────────────────────
Write-Host ""
Write-Host "[3] Retrieving storage account key..."
$accessKey = az storage account keys list --account-name $storageAccountName --resource-group $resourceGroup --query "[0].value" --output tsv
if (-not $accessKey) {
    Write-Host "ERROR: Could not retrieve storage account key."
    exit 1
}
Write-Host "    Key retrieved."

# ── Step 4: Create container ───────────────────────────────────────────────────
Write-Host ""
Write-Host "[4] Creating blob container '$containerName'..."
$createOutput = az storage container create --name $containerName --account-name $storageAccountName --account-key $accessKey --public-access off --output json
$createResult = $createOutput | ConvertFrom-Json

if ($createResult.created -eq $true) {
    Write-Host "    Container '$containerName' created successfully."
} else {
    Write-Host "    Container '$containerName' already exists or creation skipped."
}

# ── Step 5: Role assignment (best effort) ─────────────────────────────────────
Write-Host ""
Write-Host "[5] Checking role assignment for signed-in user..."

$userId = az ad signed-in-user show --query id --output tsv 2>$null

if ($userId -and $LASTEXITCODE -eq 0) {
    Write-Host "    User ID: $userId"
    $storageId = az storage account show --name $storageAccountName --resource-group $resourceGroup --query id --output tsv

    $roleCheck = az role assignment list --assignee $userId --role "Storage Blob Data Contributor" --scope $storageId --query "[].roleDefinitionName" --output tsv 2>$null

    if ($roleCheck -like "*Storage Blob Data Contributor*") {
        Write-Host "    Role 'Storage Blob Data Contributor' already assigned - OK."
    } else {
        Write-Host "    Assigning 'Storage Blob Data Contributor' role..."
        az role assignment create --role "Storage Blob Data Contributor" --assignee $userId --scope $storageId --output none
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Role assigned. (May take 1-2 minutes to propagate in Azure AD.)"
        } else {
            Write-Host "    WARNING: Role assignment failed. Assign manually if needed."
        }
    }
} else {
    Write-Host "    Could not determine signed-in user (skipping role check)."
    Write-Host "    Ensure 'Storage Blob Data Contributor' is assigned to your identity."
}

# ── Step 6: List all containers ───────────────────────────────────────────────
Write-Host ""
Write-Host "[6] Listing all containers in '$storageAccountName':"
az storage container list --account-name $storageAccountName --account-key $accessKey --query "[].name" --output tsv | ForEach-Object { Write-Host "    - $_" }

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=================================================="
Write-Host "  SETUP COMPLETE"
Write-Host "=================================================="
Write-Host ""
Write-Host "Container URL:"
Write-Host "  https://$storageAccountName.blob.core.windows.net/$containerName"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Extract Celonis tables and upload to Azure Blob:"
Write-Host "     python celonis_to_azure.py"
Write-Host ""
Write-Host "  2. Verify read-back from Azure Blob:"
Write-Host "     python azure_table_reader.py"
Write-Host ""
Write-Host "  3. Run full pipeline with Azure mapper + decision tree:"
Write-Host "     python pipeline_run.py --folder ""PO examples"" --use-azure-mapper"
Write-Host ""
