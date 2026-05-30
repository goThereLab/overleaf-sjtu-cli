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
overleaf auth login --mfa-method app --no-remember
overleaf auth pending
overleaf auth login --mfa-code APP_CODE --no-remember
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
overleaf auth login --mfa-method email --no-remember
overleaf auth pending
overleaf auth login --mfa-code EMAIL_CODE --no-remember
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
overleaf auth login --mfa-method sms --no-remember
overleaf auth pending
overleaf auth login --mfa-code SMS_CODE --no-remember
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
overleaf auth pending
overleaf auth pending --json
```

如果验证码没到或过期：

```bash
overleaf auth login --mfa-resend
```

如果验证码被拒，直接提交新验证码；失败后 pending 状态会保留：

```bash
overleaf auth login --mfa-code NEW_CODE --no-remember
```

排查时保留失败方式的完整命令输出，包括 `auth pending`，这样可以对应到 CLI 的具体状态分支。
