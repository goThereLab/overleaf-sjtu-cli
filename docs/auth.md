# 登录与 jAccount

`overleaf auth login` 通过 HTTP 跟随 jAccount 登录链路，不依赖浏览器自动化。登录成功后，Overleaf session 会保存在本机状态目录中。

## 基础登录

```bash
overleaf auth status
overleaf auth login
overleaf auth whoami
```

`auth login` 是面向人的交互式入口。如果 jAccount 要求验证码，交互式终端会直接显示验证码图像。
在 Windows 终端中，CLI 会使用 ASCII 高对比渲染代替 truecolor 色块，并额外保存一份验证码 PNG 路径，避免旧版 PowerShell/ConHost 对 ANSI 24 位颜色或半块字符支持不完整导致验证码不可读。

`auth status` 用来显式查看 session 和 pending flow：

```bash
overleaf auth status
overleaf auth status --check
overleaf auth status --json
```

## Agent/无 TTY 登录

Agent 和脚本应优先使用显式 flow 命令，而不是反复给 `auth login` 传不同阶段的参数。flow 文件中保存 jAccount 临时上下文和临时 cookie，不保存密码，文件权限为 `0600`。

第一步，启动登录并保存验证码：

```bash
overleaf auth flow start \
  --flow /tmp/overleaf-login.json \
  --captcha-output /tmp/jaccount-captcha.png
```

读取验证码图片后提交账号密码：

```bash
overleaf auth flow submit-password \
  --flow /tmp/overleaf-login.json \
  --username USERNAME \
  --password PASSWORD \
  --captcha CAPTCHA
```

如果返回 `mfa_required`，先请求一种二次认证方式：

```bash
overleaf auth flow mfa-request \
  --flow /tmp/overleaf-login.json \
  --method email
```

收到验证码后提交：

```bash
overleaf auth flow mfa-submit \
  --flow /tmp/overleaf-login.json \
  --code CODE
```

每一步都支持 `--json`，输出中会包含 `flow`、`state` 和 `next` 字段，便于 Agent 按状态机推进。

查看或取消当前 flow：

```bash
overleaf auth flow status --flow /tmp/overleaf-login.json
overleaf auth flow cancel --flow /tmp/overleaf-login.json
```

## 二次认证方式

jAccount 可能要求额外认证。当前支持三种方式：

```bash
overleaf auth flow mfa-request --method app
overleaf auth flow mfa-request --method email
overleaf auth flow mfa-request --method sms
```

如果验证码没到或过期：

```bash
overleaf auth flow resend --flow /tmp/overleaf-login.json
```

`app` 会发送到 My SJTU/交我办，`email` 会发送到 jAccount 邮箱目标，`sms` 会发送到 jAccount 显示的手机号。短时间内重复触发同一种方式可能被 jAccount 拒绝。

## Cookie 导入

如果密码登录被策略拦截，可以从已登录浏览器复制 Cookie：

```bash
overleaf auth login --cookie 'overleaf.sid=...; other_cookie=...'
```

## 凭据保存

默认情况下，工具复用本机 `canvas` 命令的 jAccount keyring 条目：

```text
service: canvas
username key: jaccount.username
password key: jaccount.password:<username>
legacy services: canvas-cli, sjtu-canvas-cli
```

密码也可以通过提示输入、`--password` 或 `OVERLEAF_PASSWORD` 提供。如果交互式输入了新密码，CLI 会询问是否记住；也可以用 `--remember` 或 `--no-remember` 显式指定。

## 登录链路

当前链路大致为：

```text
/jaccountlogin
  -> https://jaccount.sjtu.edu.cn/oauth2/authorize
  -> https://jaccount.sjtu.edu.cn/jaccount/jalogin
  -> POST https://jaccount.sjtu.edu.cn/jaccount/ulogin
  -> https://latex.sjtu.edu.cn/jaccountlogin/cb
```
