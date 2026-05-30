# overleaf-sjtu-cli

`overleaf` 是一个面向上海交通大学 Overleaf (`https://latex.sjtu.edu.cn`) 的命令行工具。

它的定位很简单：不用打开浏览器，在终端里完成登录、项目管理、文件上传下载、编译和 PDF/日志获取。适合远程服务器、SSH 环境和自动化脚本。

## 安装

从 GitHub 直接安装最新版：

```bash
python -m pip install --upgrade \
  "overleaf-sjtu-cli @ git+https://github.com/goThereLab/overleaf-sjtu-cli.git@main"
```

开发或远程调试时用 editable install：

```bash
git clone https://github.com/goThereLab/overleaf-sjtu-cli.git
cd overleaf-sjtu-cli
python -m pip install -e .
```

## Shell 补全

zsh：

```bash
overleaf completion install zsh
exec zsh
```

bash：

```bash
overleaf completion install bash
exec bash
```

如果更新后补全没有变化，重新安装补全并 source：

```bash
overleaf completion install bash
source ~/.bash_completion.d/overleaf
```

## 基础用法

登录：

```bash
overleaf auth login
overleaf auth whoami
```

列出并选择项目：

```bash
overleaf project list
overleaf project select PROJECT
```

上传 zip 为新项目：

```bash
overleaf project upload paper.zip --name paper --select
```

设置默认编译器并编译：

```bash
overleaf settings compiler xelatex
overleaf compile run
overleaf compile pdf -o output.pdf
```

临时指定编译器，不修改项目默认设置：

```bash
overleaf compile run --compiler lualatex
```

查看或下载文件：

```bash
overleaf file ls
overleaf file download main.tex -o main.tex
overleaf file upload main.tex
```

## 文档

- [完整用法](docs/usage.md)
- [登录与 jAccount 说明](docs/auth.md)
- [文件命令说明](docs/files.md)
- [MFA 手动测试清单](docs/mfa-testing.md)
