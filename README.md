# overleaf-sjtu-cli

`overleaf` is a command line client for SJTU Overleaf at `https://latex.sjtu.edu.cn/project`.

It logs in through jAccount with plain HTTP requests, saves the authenticated cookies locally, and then performs project and compile operations through HTTP requests.

## Install

```bash
pip install -e .
overleaf completion install zsh  # macOS default shell
# or
overleaf completion install bash
```

If the `overleaf` command is not found after installation, add your Python scripts directory to `PATH`. For Hammer's pyenv Python this is usually:

```bash
export PATH="$HOME/.pyenv/versions/3.10.11/bin:$PATH"
```

The zsh completion installer writes `~/.zsh/completions/_overleaf` and adds that directory to `~/.zshrc`. Restart zsh after installing completion:

```bash
exec zsh
```

The bash completion installer writes `~/.bash_completion.d/overleaf` and sources it from `~/.bashrc`. Restart bash after installing completion:

```bash
exec bash
```

To inspect or install the completion script manually:

```bash
overleaf completion show zsh
overleaf completion install zsh --path ~/.zsh/completions --no-zshrc
overleaf completion show bash
overleaf completion install bash --path ~/.bash_completion.d --no-bashrc
```

## Quick Start

```bash
overleaf auth login
overleaf project list
overleaf project upload paper.zip --name paper --select
overleaf settings compiler xelatex
overleaf compile run
overleaf compile pdf -o output.pdf
overleaf compile log
overleaf file pwd
overleaf file ls
overleaf file cd figures
overleaf file download exp1_convergence.png -o exp1_convergence.png
```

If jAccount asks for additional verification on a new device or IP, choose one of the available methods:

```bash
overleaf auth login --mfa-method app
overleaf auth login --mfa-method email
overleaf auth login --mfa-method sms
```

For staged or non-interactive use, trigger the method first, then submit the received code:

```bash
overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --mfa-method email --no-remember
overleaf auth pending
overleaf auth login --mfa-resend    # optional, if the code expired or did not arrive
overleaf auth login --mfa-code CODE --no-remember
```

If password login is blocked by another jAccount policy, import cookies copied from an authenticated session:

```bash
overleaf auth login --cookie 'overleaf.sid=...; other_cookie=...'
```

## Commands

```bash
overleaf auth whoami
overleaf auth login
overleaf auth pending
overleaf auth logout
overleaf config
overleaf completion install zsh

overleaf project list [--limit 50] [--json] [--quiet]
overleaf project show PROJECT
overleaf project create --name NAME [--select]
overleaf project upload PROJECT.zip [--name NAME] [--select]
overleaf project download [PROJECT] -o PROJECT.zip
overleaf project delete PROJECT --yes
overleaf project select PROJECT
overleaf project current
overleaf project clear

overleaf file pwd
overleaf file cd REMOTE_DIR
overleaf file   # interactive shell on a TTY; prints help without a TTY
overleaf file ls [REMOTE_PATH] [--project PROJECT] [--json] [--quiet]
overleaf file tree [REMOTE_PATH] [--depth N] [--limit N] [--all] [--project PROJECT] [--json]
overleaf file download REMOTE_PATH [-o LOCAL_PATH]
overleaf file upload LOCAL_PATH [REMOTE_PATH]
overleaf file edit REMOTE_PATH [--editor vim|nano]
overleaf file mkdir REMOTE_PATH

overleaf settings compiler xelatex [--project PROJECT]

overleaf compile run [PROJECT] [--draft] [--stop-on-first-error] [--wait 120]
overleaf compile status [PROJECT]
overleaf compile pdf [PROJECT] -o output.pdf
overleaf compile log [PROJECT] [--tail 80] [--full] [-o output.log]
```

`PROJECT` can be a 24-character Overleaf project id or a full project URL.

## State

Configuration is stored in:

```text
~/.config/overleaf-sjtu/config.json
```

Session cookies are stored in:

```text
~/.local/state/overleaf-sjtu/cookies.json
```

Cookie files are written with mode `0600`.

## Notes

`overleaf auth login` follows the discovered HTTP chain:

```text
/jaccountlogin
  -> https://jaccount.sjtu.edu.cn/oauth2/authorize
  -> https://jaccount.sjtu.edu.cn/jaccount/jalogin
  -> POST https://jaccount.sjtu.edu.cn/jaccount/ulogin
  -> https://latex.sjtu.edu.cn/jaccountlogin/cb
```

If jAccount asks for a CAPTCHA, the CLI renders it as colored terminal blocks when running on a TTY and prompts for the code. Without a TTY, the first `overleaf auth login` saves the CAPTCHA image plus its matching jAccount login context, prints a `Next:` hint, and exits without submitting credentials. Read the image, then run the hinted second login command:

```bash
overleaf auth login --captcha-output jaccount-captcha.png </dev/null
overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --no-remember
```

If the password step succeeds but jAccount asks for extra verification, the CLI supports the three methods exposed by the page: `app`, `email`, and `sms`. It calls jAccount's verification-code endpoint for the selected method, prompts for the received code, and submits it with `--trust-mfa` enabled by default. Use `--no-trust-mfa` to avoid trusting the current device.

For staged verification, first trigger the method and then submit the code in a second command:

```bash
overleaf auth login --mfa-method app
overleaf auth pending
overleaf auth login --mfa-resend    # optional, reuses the pending method
overleaf auth login --mfa-code CODE --no-remember
```

If a non-interactive login reaches additional verification before a method was selected, the challenge is kept as pending. Check the available methods, then select one without repeating the password step:

```bash
overleaf auth pending
overleaf auth login --mfa-method email --no-remember
overleaf auth login --mfa-code CODE --no-remember
```

`app` sends the code to My SJTU/交我办, `email` sends it to the jAccount mail target, and `sms` sends it to the masked phone number reported by jAccount. Avoid repeatedly triggering the same method in a short time; jAccount may reject it as too frequent.

Suggested end-to-end check for the three methods:

```bash
overleaf auth logout
overleaf auth login --mfa-method app --no-remember
overleaf auth pending
overleaf auth login --mfa-code APP_CODE --no-remember
overleaf auth whoami

overleaf auth logout
overleaf auth login --mfa-method email --no-remember
overleaf auth pending
overleaf auth login --mfa-code EMAIL_CODE --no-remember
overleaf auth whoami

overleaf auth logout
overleaf auth login --mfa-method sms --no-remember
overleaf auth pending
overleaf auth login --mfa-code SMS_CODE --no-remember
overleaf auth whoami
```

Each method should finish with `Logged in: ... visible projects`, and `overleaf auth whoami` should print `Authenticated at ...`. If a code expires or does not arrive, run `overleaf auth login --mfa-resend`; if a code is rejected, the pending state is kept so a new `--mfa-code` can be submitted without starting over.

By default `overleaf auth login` reuses the same system keyring credentials as the local `canvas` command:

```text
service: canvas
username key: jaccount.username
password key: jaccount.password:<username>
legacy services: canvas-cli, sjtu-canvas-cli
```

Passwords can also be provided via prompt, `--password`, or `OVERLEAF_PASSWORD`. `overleaf auth login` reuses saved keyring credentials automatically when they exist. If no saved credentials are available and you type a password interactively, the CLI asks whether to remember it. Use `--remember` or `--no-remember` to make that choice explicit. The Overleaf session itself is still stored separately under `~/.local/state/overleaf-sjtu/cookies.json`.

Overleaf deployments sometimes customize upload, delete, compile, and settings endpoints. The client tries common Overleaf/ShareLaTeX endpoints and reports the final HTTP error if the server differs.

File-level commands keep a remembered remote working directory per project. Use `overleaf file pwd` to print it and `overleaf file cd DIR` to change it; relative paths in `ls`, `upload`, `download`, `mkdir`, `edit`, and `tree` resolve against that directory. Running `overleaf file` in a TTY opens an interactive shell with a colored prompt, command history, and Tab completion for commands and remote paths. Inside the shell, `vim foo.tex` and `nano foo.tex` are aliases for `edit foo.tex --editor vim|nano`. Without a TTY, `overleaf file` prints help. If no current project is selected, run `overleaf project list` then `overleaf project select <id>`.

File-level downloads use the authenticated project zip endpoint and extract the requested path locally, so both editable docs and binary assets are supported. Single-file text uploads for editable docs such as `.tex`, `.bib`, `.sty`, and `.txt` are supported through Overleaf's lightweight socket.io OT channel, without browser automation. Binary assets use the HTTP upload endpoint. Directory uploads still reject editable text docs; upload text sources one file at a time or upload a project zip for bulk source replacement.
