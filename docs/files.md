# 文件命令

文件命令作用于当前项目。没有当前项目时，先选择一个项目：

```bash
overleaf project list
overleaf project select PROJECT
```

## 常用命令

```bash
overleaf file pwd
overleaf file cd REMOTE_DIR
overleaf file ls [REMOTE_PATH] [--project PROJECT] [--json] [--quiet]
overleaf file tree [REMOTE_PATH] [--depth N] [--limit N] [--all] [--project PROJECT] [--json]
overleaf file download REMOTE_PATH [-o LOCAL_PATH]
overleaf file upload LOCAL_PATH [REMOTE_PATH]
overleaf file edit REMOTE_PATH [--editor vim|nano]
overleaf file mkdir REMOTE_PATH
```

`overleaf file cd DIR` 会为每个项目记住远程工作目录。`ls`、`upload`、`download`、`mkdir`、`edit` 和 `tree` 的相对路径都会基于这个目录解析。

## 交互式 shell

在 TTY 里直接运行：

```bash
overleaf file
```

会进入一个简易文件 shell，带命令历史和 Tab 补全。里面的 `vim foo.tex`、`nano foo.tex` 是 `edit foo.tex --editor vim|nano` 的别名。

非 TTY 下运行 `overleaf file` 会打印帮助。

## 上传与下载

下载通过认证后的项目 zip 端点完成，然后在本地提取请求路径，所以文本源文件和二进制资源都支持。

单个 `.tex`、`.bib`、`.sty`、`.txt` 等可编辑文本文件，会通过 Overleaf 的 socket.io OT 通道上传。图片等二进制资源使用 HTTP 上传端点。

目录上传暂不支持批量替换可编辑文本源文件。需要批量替换源码时，建议上传项目 zip；需要小范围修改时，一次上传一个文本文件。
