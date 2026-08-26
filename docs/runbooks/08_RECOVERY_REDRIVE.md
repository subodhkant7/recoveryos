# Runbook 08: Operator Recovery & Redrive Procedures

## 1. Scope & Purpose
Safe operational procedure for redriving stuck or stalled workflows using the authenticated Operator Recovery API (`POST /api/workflows/{id}/recover`).

## 2. Safety Rules Before Redrive
1. **Never redrive healthy workflows**: Verify the workflow is actually stuck ($> 300\text{s}$ inactive or failed).
2. **Verify tenant identity**: Redrive requests must authenticate with an `OPERATOR` or `ADMIN` JWT matching the workflow's `tenant_id`.
3. **Immutable terminal states**: Completed or Escalated workflows cannot be redrived (returns HTTP 400).
4. **OCC version fencing**: The recovery endpoint automatically fences redrive dispatch to the current workflow OCC version.

## 3. Redrive Execution Steps
```bash
python3 -c "
import subprocess, httpx, json
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
jwt_secret = subprocess.check_output(['gcloud', 'secrets', 'versions', 'access', 'latest', '--secret=recoveryos-jwt-secret', '--project=recoveryos-506713']).decode().strip()

from backend.security.tokens import create_access_token
from backend.security.principal import Role

tenant_id = '<TENANT_ID>'
workflow_id = '<WORKFLOW_ID>'

op_jwt = create_access_token('operator-redrive', Role.OPERATOR, tenant_id=tenant_id, secret_key=jwt_secret)
headers = {'X-Serverless-Authorization': f'Bearer {id_token}', 'Authorization': f'Bearer {op_jwt}'}

# Step 1: Pre-flight diagnostics
diag = httpx.get(f'https://recoveryos-321161003794.asia-east1.run.app/api/workflows/{workflow_id}/diagnostics', headers=headers).json()
print('Diagnostics:', json.dumps(diag, indent=2))
assert diag['is_recoverable'], 'Workflow is not in a recoverable state'

# Step 2: Trigger Recovery Redrive
res = httpx.post(
    f'https://recoveryos-321161003794.asia-east1.run.app/api/workflows/{workflow_id}/recover',
    json={'reason': 'Operator redrive following stalled execution'},
    headers=headers,
)
print('Recovery dispatch result:', res.status_code, res.json())
assert res.status_code == 202
"
```

## 4. Verification
- Verify timeline event `RECOVERY_DISPATCH` appended in Firestore.
- Check worker logs for `Consuming workflow execution message` with `event_type: RECOVERY_TRIGGER`.
