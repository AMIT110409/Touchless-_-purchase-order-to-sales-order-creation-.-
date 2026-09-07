#!/bin/bash
# Azure Setup Script for PO Extraction Agent
# Usage: ./azure-setup.sh

# Exit on error
set -e

# Config Variables
RESOURCE_GROUP="rg-po-extraction-prod"
LOCATION="westeurope"
ACR_NAME="poextractionacr$RANDOM"
STORAGE_ACCOUNT="poextractionstore$RANDOM"
KEY_VAULT="poextractionkv$RANDOM"
DB_SERVER_NAME="poextractiondb$RANDOM"
ACA_ENV="po-extraction-env"
CONTAINER_APP_NAME="smart-po-extraction"
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

echo "starting deployment..."

# 1. Create Resource Group
echo "Creating Resource Group: $RESOURCE_GROUP..."
az group create --name $RESOURCE_GROUP --location $LOCATION

# 2. Azure Container Registry (ACR)
echo "Creating ACR: $ACR_NAME..."
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --admin-enabled true

# 3. Storage Account
echo "Creating Storage Account: $STORAGE_ACCOUNT..."
az storage account create --name $STORAGE_ACCOUNT --resource-group $RESOURCE_GROUP --location $LOCATION --sku Standard_LRS

# Get Storage Key
STORAGE_KEY=$(az storage account keys list --resource-group $RESOURCE_GROUP --account-name $STORAGE_ACCOUNT --query '[0].value' -o tsv)

# Create Containers
echo "Creating Blob Containers..."
az storage container create --name "input-po" --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY
az storage container create --name "processed-json" --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY
az storage container create --name "failed" --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY

# Create Queue
echo "Creating Queue..."
az storage queue create --name "po-processing-queue" --account-name $STORAGE_ACCOUNT --account-key $STORAGE_KEY

# 4. Azure Database for PostgreSQL (Flexible Server)
echo "Creating PostgreSQL Flexible Server: $DB_SERVER_NAME..."
# Note: This prompts for admin password if not provided. For automation, better to generate one.
DB_PASSWORD="Password1234!" # REPLACE THIS IN PROD!
az postgres flexible-server create --resource-group $RESOURCE_GROUP \
    --name $DB_SERVER_NAME \
    --location $LOCATION \
    --admin-user "poadmin" \
    --admin-password "$DB_PASSWORD" \
    --sku-name Standard_B1ms \
    --tier Burstable \
    --storage-size 32 \
    --yes

# Allow access from Azure services
az postgres flexible-server firewall-rule create --resource-group $RESOURCE_GROUP --name $DB_SERVER_NAME --rule-name allow-azure --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0

# 5. Azure Key Vault (Optional but recommended)
echo "Creating Key Vault: $KEY_VAULT..."
az keyvault create --name $KEY_VAULT --resource-group $RESOURCE_GROUP --location $LOCATION

# 6. Container Apps Environment (With GPU profile if available in region)
echo "Creating Container Apps Environment: $ACA_ENV..."
az containerapp env create --name $ACA_ENV --resource-group $RESOURCE_GROUP --location $LOCATION --enable-workload-profiles

# 7. Create User Assigned Identity for the App
IDENTITY_NAME="id-po-extraction"
echo "Creating Managed Identity: $IDENTITY_NAME..."
az identity create --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP
IDENTITY_ID=$(az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query id -o tsv)
IDENTITY_CLIENT_ID=$(az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query clientId -o tsv)
PRINCIPAL_ID=$(az identity show --name $IDENTITY_NAME --resource-group $RESOURCE_GROUP --query principalId -o tsv)

# 8. Assign Roles
# Storage Blob Data Contributor
az role assignment create --assignee $PRINCIPAL_ID \
    --role "Storage Blob Data Contributor" \
    --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT"

# Storage Queue Data Contributor
az role assignment create --assignee $PRINCIPAL_ID \
    --role "Storage Queue Data Contributor" \
    --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT"

# ACR Pull
az role assignment create --assignee $PRINCIPAL_ID \
    --role "AcrPull" \
    --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ContainerRegistry/registries/$ACR_NAME"

echo "--------------------------------------------------"
echo "Deployment Complete!"
echo "ACR Name: $ACR_NAME"
echo "Storage Account: $STORAGE_ACCOUNT"
echo "Postgres Server: $DB_SERVER_NAME.postgres.database.azure.com"
echo "Identity Client ID: $IDENTITY_CLIENT_ID"
echo "Please update your deployment YAML and application config with these values."
echo "--------------------------------------------------"
