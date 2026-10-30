# Password-protected assessment demo

This patch adds a shared demo access credential to the existing application. It
changes no database schema, worker lifecycle, Gemini provider, invoice extraction,
correction history, or retry transaction logic. Migration 0004 remains current.
There are no dependency changes and no environment files in this package.

## Deploy on Render and Vercel

1. Generate a random password using the PowerShell command below or a cryptographic
   password generator. The value must contain 32–256 characters drawn from letters,
   digits, underscore and hyphen. The supplied generator produces 43 characters
   from 32 random bytes. Do not use a test fixture credential.
2. Open your existing Render backend service, then Environment. Add the settings
   in the following table. Choose Save only while preparing the code update;
   these values take effect on the next deploy. Render documents these options in
   [Environment Variables and Secrets](https://render.com/docs/configure-environment-variables).
3. Apply this ZIP to the repository root, run the verification commands, and deploy
   the changed backend code through the existing Render build/start configuration.
   After pushing the tested commit, use Deploys > Manual Deploy > Deploy latest
   commit if an automatic deploy has not started. Confirm that the deployed commit
   contains this patch. See [Render deployment instructions](https://render.com/docs/deploys).
   Keep the current database, storage, Gemini settings, port, and start command.
4. Set the Render health-check path to /api/health. Confirm that health returns 200
   and unauthenticated document reads AND mutations return 401. Do not consider
   the public service protected until these live checks pass.
5. Deploy the changed frontend to the existing Vercel project. Keep
   VITE_API_BASE_URL set to the HTTPS Render backend origin, without /api.
   Do not add DOCUMENT_API_TOKEN or any VITE_ password/token variable to Vercel.
6. Open the Vercel application and paste the password into the access screen.
   Share the credential with the reviewer separately from source code.

| Backend environment setting | Required value |
|---|---|
| APP_ENV | production |
| DOCUMENT_API_TOKEN | Your newly generated random demo password |
| DOCUMENT_AUTH_DISABLED | false, or leave unset |
| DEBUG | false |
| CORS_ORIGINS | JSON array containing the exact HTTPS Vercel origin(s) |

Use the existing correct CORS_ORIGINS value if it already names your frontend.
Do not use a wildcard or include a URL path. Authorization is already in the
backend's permitted CORS headers. Each preview origin needs an explicit entry.

Render sets RENDER=true; this patch treats that as production even if APP_ENV was
accidentally left as development/test. See Render's
[default environment variables](https://render.com/docs/environment-variables).
Missing, blank, malformed, or too-short tokens fail API startup. A production
disable flag also fails startup. An unsuccessful deploy may leave an older
version serving traffic: verify the actual public URL after deployment.

The backend token is configuration on the API service. A separate worker service
does not make authenticated HTTP document requests and needs no code change.
Existing queued jobs can continue; browser logout does not cancel accepted work.

## Generate a credential without printing it

Run this yourself in a private PowerShell terminal. It places the new token in
that terminal's backend environment and the clipboard for pasting into Render
and the reviewer's access screen. It does not write a file or print the token.

~~~powershell
$demoBytes = New-Object byte[] 32
$demoRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $demoRng.GetBytes($demoBytes)
} finally {
    $demoRng.Dispose()
}
$env:DOCUMENT_API_TOKEN = [Convert]::ToBase64String($demoBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
Set-Clipboard -Value $env:DOCUMENT_API_TOKEN
[Array]::Clear($demoBytes, 0, $demoBytes.Length)
Remove-Variable demoBytes, demoRng
~~~

After pasting/sharing through your chosen secure channel, clear the clipboard:

~~~powershell
Set-Clipboard -Value ''
~~~

Do not paste the token into commits, screenshots, URLs, terminal commands as a
literal value, frontend environment settings, or application logs.

## Local API startup with authentication

From the repository root in the backend terminal, after generating a credential
as above (or loading your own backend token into this process environment):

~~~powershell
Set-Location .\backend
$env:APP_ENV = 'development'
$env:DOCUMENT_AUTH_DISABLED = 'false'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
~~~

Use another terminal for your existing worker command:

~~~powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m app.workers --processor app.services.invoice_pipeline:process_document
~~~

Use a separate frontend terminal, with no token environment variable:

~~~powershell
Set-Location .\frontend
npm run dev
~~~

The existing Vite /api proxy works with VITE_API_BASE_URL=/ locally. An explicit
http://127.0.0.1:8000 backend also works locally with the correct CORS origin.
The authenticated client rejects remote HTTP origins and accepts HTTPS for
hosted use. It refuses HTTP redirects so credentials and mutation bodies are
not silently forwarded. Use the backend's final canonical URL.

## Explicit test/development modes

- Production or Render: authentication is mandatory; missing token or a true
  DOCUMENT_AUTH_DISABLED value fails startup.
- Development: authentication is the default. Only an explicit
  DOCUMENT_AUTH_DISABLED=true, with no configured token, permits local API bypass.
- Test: explicitly configuring APP_ENV=test without a token preserves isolated
  legacy API tests. A supplied token is still enforced. Render never permits
  this bypass.
- A configured token is always validated/enforced in every mode. A blank token
  is a configuration error, not an instruction to bypass authentication.

The access-check endpoint returns authenticated=false during explicit local/test
bypass, so the frontend does not claim that an arbitrary password was verified.
Configure a token for the full frontend login experience. The new pytest
conftest fixture isolates security-related environment variables from tests; the
security tests supply their own synthetic credentials explicitly.

## API and browser behavior

The auth middleware runs before upload buffering, multipart/JSON parsing and
route dependencies. It protects all non-public HTTP paths, including future
document APIs. Public GET/HEAD paths are /api/health and the informational
/docs, /redoc and /openapi.json pages. Swagger marks document operations with a
Bearer security scheme and does not persist its authorization value.

The client constructs the Authorization header from the literal Bearer, one
space, and the password entered by the reviewer. Never insert the real value
into a tracked file. URL/query credentials, duplicate Authorization headers, malformed
Bearer values and wrong tokens are rejected. The server compares fixed-length
SHA-256 digests with hmac.compare_digest and emits a generic structured 401 with
WWW-Authenticate: Bearer. The guard does not log submitted credentials.

The new GET /api/auth/check endpoint uses the same guard and returns
{"authenticated": true} for valid access. It performs no database lookup,
upload, queue operation or Gemini call. Existing document response shapes and
mutation semantics are unchanged.

CORSMiddleware wraps the authentication guard. Valid allowed-origin OPTIONS
preflights do not need credentials and cannot modify documents. Ordinary OPTIONS
requests are not an authentication bypass. CORS headers are present on allowed-
origin 401 responses, allowing the frontend to recognize expired/invalid access.
Protected responses use Cache-Control: no-store and Vary: Authorization; browser
requests also use cache: no-store and credentials: omit.

The reviewer password is held in a closure in this tab's memory. No localStorage,
sessionStorage, cookie, URL or VITE_ variable stores it. The main entry point
always mounts the access screen before the document app. Every API method uses
the same Authorization-header transport.

Lock workspace, page leave/reload, or 30 minutes without user interaction clears
the credential, unmounts document views and aborts pending browser requests.
Polling does not extend the inactivity deadline. Returning to a throttled tab
also checks the deadline. A 401 performs the same cleanup; late responses from an
old session cannot log out a newer one or republish stale document data.

Upload validation/size errors (for example 413/415/422), server outages and
transient network errors are not treated as invalid credentials. The existing
upload outcome-unknown warning remains. POST requests are never automatically
replayed after reauthentication. An already accepted request may have committed
before the browser aborts it; inspect current results before resubmitting.

This is a shared demo credential. Anyone who has it can read documents and use
review/reprocessing functions. Reviewer labels in correction history remain
self-reported, not verified individual identities. The 30-minute browser lock
is not server-side credential revocation. To revoke the shared token, replace
DOCUMENT_API_TOKEN on Render and choose Save and deploy; old tokens then receive
401 on updated instances. Do not use Render Restart service to apply a token
change: it reuses the environment values from the existing deployment. No JWT, user database, or password-reset system is introduced.

## Automated verification

No new dependencies or database migrations are required. From the repository root:

~~~powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m pytest tests/test_demo_access.py -q
.\.venv\Scripts\python.exe -m pytest tests -q --cov=app --cov-report=term-missing
.\.venv\Scripts\python.exe -m alembic check
Set-Location ..\frontend
npm run test -- src/test/access.test.tsx
npm run verify
Set-Location ..
~~~

Keep the existing frontend build configuration and backend dependencies.
INVOICE_RUN_LIVE_TESTS=1 still opts into the existing billable Gemini test; the
new auth tests use only synthetic credentials and the controlled fake provider.

Backend tests cover every document path, startup configuration, constant-time
comparison, malformed/duplicate/query credentials, CORS, pre-body rejection,
unauthorized correction/retry/upload, zero queued Gemini work after unauthorized
requests, authorized processing, no-store headers, OpenAPI and token rotation.
Frontend tests cover bearer headers on all methods, no storage, initial gating,
invalid credentials, lock/logout, reload, inactivity, pending-request cancellation,
stale responses, upload failures and prevention of automatic POST replay.

## Live smoke checks

Use a PowerShell terminal with your backend token already in DOCUMENT_API_TOKEN.
The variable is expanded in memory; the commands contain no literal secret.

~~~powershell
$demoApiBase = (Read-Host 'Backend HTTPS origin, without /api').TrimEnd('/')
Invoke-RestMethod -Uri ($demoApiBase + '/api/health')

try {
    Invoke-WebRequest -Uri ($demoApiBase + '/api/documents') -UseBasicParsing
    throw 'Security check failed: unauthenticated document access was accepted.'
} catch {
    if ($null -eq $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 401) { throw }
    Write-Output 'Unauthenticated document read correctly returned 401.'
}

$demoHeaders = @{ Authorization = ('Bearer ' + $env:DOCUMENT_API_TOKEN) }
Invoke-RestMethod -Uri ($demoApiBase + '/api/auth/check') -Headers $demoHeaders
Remove-Variable demoHeaders
~~~

Also check the actual Vercel UI: wrong password denied; correct password opens
the inbox; uploads/corrections/retries work; Lock workspace removes document
data; refresh asks for the password again. Rotate the token and confirm that
the next protected request locks an already-open tab. Verify allowed-origin
preflight and unauthenticated POST requests in browser/API tools.

## Verification status of this delivery

The project owner reported the pre-patch baseline as 273 backend and 40 frontend
tests passing, with migration 0004 applied. These are not results for this patch.
The assistant inspected current repository files read-only and prepared complete
changed/new files. No pytest, Vitest, build, migration, live HTTP, Render, Vercel,
or Gemini verification was executed for this patch. Run the commands above and
record the actual results before declaring deployment verified.

## Git and delivery

The ZIP is a delivery artifact, not a repository dependency. It contains only
the files below; do not stage the archive, credentials, environment files,
node_modules, uploaded documents or databases. After verification, an appropriate
commit message is: fix: protect document APIs with demo access authentication

### Changed/new files

- NEW — backend/app/core/auth.py
- MODIFIED — backend/app/core/config.py
- MODIFIED — backend/app/main.py
- NEW — backend/tests/conftest.py
- NEW — backend/tests/test_demo_access.py
- NEW — docs/demo-access-security.md
- MODIFIED — frontend/src/App.tsx
- NEW — frontend/src/DemoAccess.tsx
- NEW — frontend/src/access.css
- MODIFIED — frontend/src/api.ts
- NEW — frontend/src/demoSession.ts
- MODIFIED — frontend/src/main.tsx
- NEW — frontend/src/test/access.test.tsx
