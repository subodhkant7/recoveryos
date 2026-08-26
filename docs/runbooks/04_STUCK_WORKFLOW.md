# Runbook 04: Stuck Workflow Identification & Safe Recovery

## 1. Symptoms
- Workflow remains in non-terminal state (`CREATED`, `EXECUTING`, `WAITING_APPROVAL`) for $> 300$ seconds without active lease.
- Diagnostics endpoint reports `is_stuck: true`.

## 2. Diagnosis & Inspection Commands
```bash
# Call diagnostics endpoint using authorized Operator token:
python3 -c "
import subprocess, httpx
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
jwt_secret = subprocess.check_output(['gcloud', 'secrets', 'versions', 'access', 'latest', '--secret=recoveryos-jwt-secret', '--project=recoveryos-506713']).decode().strip()

from backend.security.tokens import create_access_token
from backend.security.principal import Role

op_jwt = create_access_token('operator-runbook', Role.OPERATOR, tenant_id='<TENANT_ID>', secret_key=jwt_secret)
headers = {'X-Serverless-Authorization': f'Bearer {id_token}', 'Authorization': f'Bearer {op_jwt}'}

r = httpx.get('https://recoveryos-321161003794.asia-east1.run.app/api/workflows/<WORKFLOW_ID>/diagnostics', headers=headers)
print('Diagnostics:', r.status_code, r.json())
"
```

## 3. Decision Criteria
- If `is_stuck == true` and `is_recoverable == true`: Proceed with operator redrive.
- If workflow is in terminal state (`COMPLETED` or `ESCALATED`): Do NOT trigger recovery (immutable).
- If workflow has an active unexpired lease (`operation_claim` is active): Wait until lease expires (60s).

## 4. Remediation Steps (Safe Redrive)
```bash
python3 -c "
import subprocess, httpx
id_token = subprocess.check_output(['gcloud', 'auth', 'print-identity-token']).decode().strip()
jwt_secret = subprocess.check_output(['gcloud', 'secrets', 'versions', 'access', 'latest', '--secret=recoveryos-jwt-secret', '--project=recoveryos-506713']).decode().strip()

from backend.security.tokens import create_access_token
from backend.security.principal import Role

op_jwt = create_access_token('operator-runbook', Role.OPERATOR, tenant_id='<TENANT_ID>', secret_key=jwt_secret)
headers = {'X-Serverless-Authorization': f'Bearer {id_token}', 'Authorization': f'Bearer {op_jwt}'}

payload = {'reason': 'Stuck workflow operator recovery redrive'}
r = httpx.post('https://recoveryos-321161003794.asia-east1.run.app/api/workflows/<WORKFLOW_ID>/recover', json=payload, headers=headers)
print('Recovery response:', r.status_code, r.json())
assert r.status_code == 202
"
```

## 5. Verification
- Fetch `GET /api/workflows/{workflow_id}` and confirm OCC version increments and new event is appended.
- Verify worker processes the redrive message to completion.

## 6. Escalation Condition
- Redrived workflow fails repeatedly or enters `ESCALATED` state.
