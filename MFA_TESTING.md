# jAccount MFA manual test checklist

Use this checklist on the remote machine after installing the current tree:

```bash
cd ~/workspace/hanmo/overleaf-sjtu-cli
python -m pip install -e '.[test]'
python -m pytest -q
```

Run the three methods separately. Start each method from a clean saved Overleaf
session so the login flow reaches jAccount again.

## App

```bash
overleaf auth logout
overleaf auth login --mfa-method app --no-remember
overleaf auth pending
overleaf auth login --mfa-code APP_CODE --no-remember
overleaf auth whoami
```

Expected evidence:

```text
Logged in: ... visible projects
Authenticated at ...
```

## Email

```bash
overleaf auth logout
overleaf auth login --mfa-method email --no-remember
overleaf auth pending
overleaf auth login --mfa-code EMAIL_CODE --no-remember
overleaf auth whoami
```

Expected evidence:

```text
Logged in: ... visible projects
Authenticated at ...
```

## SMS

```bash
overleaf auth logout
overleaf auth login --mfa-method sms --no-remember
overleaf auth pending
overleaf auth login --mfa-code SMS_CODE --no-remember
overleaf auth whoami
```

Expected evidence:

```text
Logged in: ... visible projects
Authenticated at ...
```

## If a method stalls

Check the pending state:

```bash
overleaf auth pending
overleaf auth pending --json
```

If the code did not arrive or expired:

```bash
overleaf auth login --mfa-resend
```

If the code was rejected, submit the new code directly; the pending state is
kept after a failed submission:

```bash
overleaf auth login --mfa-code NEW_CODE --no-remember
```

Paste the full command output for the failing method, including `auth pending`,
so the failing branch can be matched to the CLI state.
