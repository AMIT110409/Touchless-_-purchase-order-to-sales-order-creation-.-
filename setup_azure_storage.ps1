# Azure Storage Setup for PO Extraction
# Requires Azure CLI: 'az login'

$resourceGroup = "RG-WE-PROD-TouchlessOrderCapture-01"
$location = "westeurope"

# Storage account names must be globally unique, 3-24 characters, numbers and lowercase letters only.
$rand = Get-Random -Minimum 10000 -Maximum 99999
$storageAccountName = "poextstorage$rand"
$containerName = "input-po"
$queueName = "po-processing-queue"

Write-Host "Using existing Resource Group: $resourceGroup in $location..."

Write-Host "Creating Storage Account: $storageAccountName..."
az storage account create --name $storageAccountName --resource-group $resourceGroup --location $location --sku Standard_LRS --allow-blob-public-access false | Out-Null

Write-Host "Retrieving Storage Account ID for Role Assignment..."
$storageId = az storage account show --name $storageAccountName --resource-group $resourceGroup --query id -o tsv

Write-Host "Getting current signed-in Azure user..."
$userId = az ad signed-in-user show --query id -o tsv

Write-Host "Assigning 'Storage Blob Data Contributor' right to your user (so DefaultAzureCredential works locally)..."
# Note: Role assignment might take a minute or two to propagate in Azure AD
az role assignment create --role "Storage Blob Data Contributor" --assignee $userId --scope $storageId | Out-Null

Write-Host "Assigning 'Storage Queue Data Contributor' right to your user..."
az role assignment create --role "Storage Queue Data Contributor" --assignee $userId --scope $storageId | Out-Null

Write-Host "Creating Blob Container: $containerName..."
# Using account key to create container because RBAC assignment takes a few minutes to propagate
$accessKey = az storage account keys list --account-name $storageAccountName --resource-group $resourceGroup --query "[0].value" -o tsv
az storage container create --name $containerName --account-name $storageAccountName --account-key $accessKey | Out-Null

Write-Host "Creating Storage Queue: $queueName..."
az storage queue create --name $queueName --account-name $storageAccountName --account-key $accessKey | Out-Null

$blobUrl = "https://$storageAccountName.blob.core.windows.net"
$queueUrl = "https://$storageAccountName.queue.core.windows.net/$queueName"

Write-Host ""
Write-Host "---------------------------------------------------"
Write-Host "STORAGE SETUP COMPLETE!"
Write-Host "---------------------------------------------------"
Write-Host "Please update your .env with these values:"
Write-Host ""
Write-Host "AZURE_BLOB_URL=$blobUrl"
Write-Host "AZURE_QUEUE_URL=$queueUrl"
Write-Host ""
Write-Host "IMPORTANT: The script assigned your Azure user the required roles to access this storage."
Write-Host "Role assignments can take 1-5 minutes to take effect in Azure. If your python script gets a 403 Forbidden error, just wait a couple of minutes and try again."
