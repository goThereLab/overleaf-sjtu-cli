# 完整用法

## 常用命令

```bash
overleaf auth whoami
overleaf auth status [--check] [--json]
overleaf auth login
overleaf auth logout
overleaf auth flow start [--flow FLOW.json] [--captcha-output captcha.png] [--json]
overleaf auth flow status [--flow FLOW.json] [--json]
overleaf auth flow submit-password --flow FLOW.json --username USER --password PASS [--captcha CODE] [--json]
overleaf auth flow mfa-request --flow FLOW.json --method [app|email|sms] [--json]
overleaf auth flow mfa-submit --flow FLOW.json --code CODE [--json]
overleaf auth flow resend --flow FLOW.json [--json]
overleaf auth flow cancel [--flow FLOW.json]
overleaf config

overleaf project list [--limit 50] [--json] [--quiet]
overleaf project show PROJECT
overleaf project create --name NAME [--select]
overleaf project upload PROJECT.zip [--name NAME] [--select]
overleaf project download [PROJECT] -o PROJECT.zip
overleaf project delete PROJECT --yes
overleaf project select PROJECT
overleaf project current
overleaf project clear

overleaf settings compiler [latex|lualatex|pdflatex|xelatex] [--project PROJECT]

overleaf compile run [PROJECT] [--compiler latex|lualatex|pdflatex|xelatex] [--draft] [--stop-on-first-error] [--wait 120]
overleaf compile status [PROJECT]
overleaf compile pdf [PROJECT] -o output.pdf
overleaf compile log [PROJECT] [--tail 80] [--full] [-o output.log]
```

`PROJECT` 可以是 24 位 Overleaf project id，也可以是完整项目 URL。未提供 `PROJECT` 时，命令会使用当前选中的项目。

## 状态文件

配置文件：

```text
~/.config/overleaf-sjtu/config.json
```

登录 Cookie：

```text
~/.local/state/overleaf-sjtu/cookies.json
```

默认登录 flow：

```text
~/.local/state/overleaf-sjtu/login_flow.json
```

兼容旧登录流程的 pending state：

```text
~/.local/state/overleaf-sjtu/login_state.json
```

Cookie 文件会以 `0600` 权限写入。

## 编译器行为

持久修改项目默认编译器：

```bash
overleaf settings compiler xelatex
```

只在本次编译临时覆盖：

```bash
overleaf compile run --compiler lualatex
```

`compile run --compiler` 不会保存设置。这样可以安全测试不同编译器，不会意外改变项目默认值。

## 补全脚本

查看脚本内容：

```bash
overleaf completion show zsh
overleaf completion show bash
```

安装到自定义目录：

```bash
overleaf completion install zsh --path ~/.zsh/completions --no-zshrc
overleaf completion install bash --path ~/.bash_completion.d --no-bashrc
```
