# CI/CD Setup (GitHub Actions)

This project now includes:

- `.github/workflows/ci.yml` (lint + tests on PR/push)
- `.github/workflows/cd.yml` (build/push Docker images to ECR + direct deploy to EC2)

## 1) GitHub Secrets (Repository)

Add these in `Settings -> Secrets and variables -> Actions -> Secrets`:

- `AWS_GITHUB_ACTIONS_ROLE_ARN`  
  IAM role assumed by GitHub OIDC for ECR push.
- `EC2_HOST`  
  Public IP or DNS of EC2.
- `EC2_USER`  
  Usually `ubuntu`.
- `EC2_SSH_PRIVATE_KEY`  
  Private key content for the EC2 key pair (full PEM text).
- `EC2_PORT` (optional)  
  Default is `22`.

## 2) GitHub Variables (Repository)

Add these in `Settings -> Secrets and variables -> Actions -> Variables`:

- `AWS_REGION` (for example `us-east-1`)
- `AWS_SECRETS_MANAGER_REGION` (usually same as region)
- `AWS_SECRETS_MANAGER_SECRET_ID` (for example `unigraph/prod/app`)
- `API_WORKERS` (for example `1`)
- `EC2_APP_DIR` (for example `~/AI-system-design`)

## 3) GitHub Environment Protection (Recommended)

Create environment `production` and require manual approval for deployments.

The CD workflow uses:

- `environment: production`

## 4) AWS IAM for GitHub OIDC Role

Attach permissions for ECR push (minimum practical set):

- `ecr:GetAuthorizationToken`
- `ecr:BatchCheckLayerAvailability`
- `ecr:CompleteLayerUpload`
- `ecr:InitiateLayerUpload`
- `ecr:UploadLayerPart`
- `ecr:PutImage`
- `ecr:BatchGetImage`

Scope repository ARNs to:

- `arn:aws:ecr:<region>:<account-id>:repository/unigraph-app`
- `arn:aws:ecr:<region>:<account-id>:repository/unigraph-gradio`

OIDC trust should allow `token.actions.githubusercontent.com` for this repo/branch.

## 5) EC2 Role Requirements

The EC2 instance role should have:

- ECR read access (for image pull)
- Secrets Manager read access for your app secret (`GetSecretValue`)

The workflow deploy step runs directly on EC2 and executes:

- ECR login
- `docker compose -f docker-compose.prod.yml --profile gradio pull api worker gradio`
- `docker compose -f docker-compose.prod.yml --profile gradio up -d api worker gradio`
- health checks for API (`/healthz`) and gradio (`:7860`)

## 6) PEM Key Notes

- Never commit PEM files into the repo.
- Store PEM only in `EC2_SSH_PRIVATE_KEY` secret.
- If your key rotates, update this secret immediately.

## 7) Deployment Flow

On push to `main`:

1. Build app + gradio images (`linux/amd64`)
2. Push to ECR with tags:
   - `sha-<12-char-commit>`
   - `latest`
3. SSH to EC2, pull latest `main`, login to ECR, run compose pull/up, and run health checks.

Manual trigger:

- `Run workflow` for CD (same full deployment flow as push-to-main).
