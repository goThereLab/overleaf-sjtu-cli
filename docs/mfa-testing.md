# jAccount MFA 手动测试清单

在远程机器安装当前代码后，用这个清单分别测试三种二次认证方式：

```bash
cd ~/workspace/hanmo/overleaf-sjtu-cli
python -m pip install -e '.[test]'
python -m pytest -q
```

三种方式需要分别测试。每次开始前都先清掉 Overleaf session，让登录流程重新进入 jAccount。

## App

```bash
overleaf auth logout
overleaf auth flow start --flow /tmp/overleaf-app.json --captcha-output /tmp/overleaf-app-captcha.png
overleaf auth flow submit-password --flow /tmp/overleaf-app.json --username USERNAME --password PASSWORD --captcha CAPTCHA
overleaf auth flow mfa-request --flow /tmp/overleaf-app.json --method app
overleaf auth flow mfa-submit --flow /tmp/overleaf-app.json --code APP_CODE
overleaf auth whoami
```

预期输出：

```text
Logged in: ... visible projects
Authenticated at ...
```

## Email

```bash
overleaf auth logout
overleaf auth flow start --flow /tmp/overleaf-email.json --captcha-output /tmp/overleaf-email-captcha.png
overleaf auth flow submit-password --flow /tmp/overleaf-email.json --username USERNAME --password PASSWORD --captcha CAPTCHA
overleaf auth flow mfa-request --flow /tmp/overleaf-email.json --method email
overleaf auth flow mfa-submit --flow /tmp/overleaf-email.json --code EMAIL_CODE
overleaf auth whoami
```

预期输出：

```text
Logged in: ... visible projects
Authenticated at ...
```

## SMS

```bash
overleaf auth logout
overleaf auth flow start --flow /tmp/overleaf-sms.json --captcha-output /tmp/overleaf-sms-captcha.png
overleaf auth flow submit-password --flow /tmp/overleaf-sms.json --username USERNAME --password PASSWORD --captcha CAPTCHA
overleaf auth flow mfa-request --flow /tmp/overleaf-sms.json --method sms
overleaf auth flow mfa-submit --flow /tmp/overleaf-sms.json --code SMS_CODE
overleaf auth whoami
```

预期输出：

```text
Logged in: ... visible projects
Authenticated at ...
```

## 如果流程卡住

先检查 pending 状态：

```bash
overleaf auth flow status --flow /tmp/overleaf-email.json
overleaf auth flow status --flow /tmp/overleaf-email.json --json
```

如果验证码没到或过期：

```bash
overleaf auth flow resend --flow /tmp/overleaf-email.json
```

如果验证码被拒，直接提交新验证码；失败后 pending 状态会保留：

```bash
overleaf auth flow mfa-submit --flow /tmp/overleaf-email.json --code NEW_CODE
```

排查时保留失败方式的完整命令输出，包括 `auth flow status --json`，这样可以对应到 CLI 的具体状态分支。
