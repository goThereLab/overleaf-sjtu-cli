# 登录与 jAccount

`overleaf auth login` 通过 HTTP 跟随 jAccount 登录链路，不依赖浏览器自动化。登录成功后，Overleaf session 会保存在本机状态目录中。

## 基础登录

```bash
overleaf auth login
overleaf auth whoami
```

如果 jAccount 要求验证码，交互式终端会直接显示验证码图像。非交互环境可以先保存验证码图片，再带验证码继续：

```bash
overleaf auth login --captcha-output jaccount-captcha.png </dev/null
overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --no-remember
```

## 二次认证

jAccount 可能要求额外认证。当前支持三种方式：

```bash
overleaf auth login --mfa-method app
overleaf auth login --mfa-method email
overleaf auth login --mfa-method sms
```

分步流程：

```bash
overleaf auth login --mfa-method email --no-remember
overleaf auth pending
overleaf auth login --mfa-code CODE --no-remember
```

如果验证码没到或过期：

```bash
overleaf auth login --mfa-resend
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
