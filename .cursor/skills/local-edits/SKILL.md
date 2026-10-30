---
name: local-edits
description: >-
  每次写代码改代码都要考虑到线上，然后在本地写和改。用户说线上、服务器、远程，也不是去改服务器的许可。Use when writing or editing
  code, fixing a bug, or when the user says 写代码、改代码、线上、服务器、远程、不许动线上.
---

# 考虑线上，只改本地

每次写代码改代码都要考虑到线上然后在本地写和改。

改 `D:\fastapi-project\Ad_Helper` 里的文件。用户说「线上」「服务器」「远程」，只表示现象出在线上，或者写的时候要按线上环境考虑。这不是授权。

不许 SSH。不许改服务器上的文件。不许重启线上进程。不许在服务器上执行 git restore、git checkout、git pull、git commit。看见线上报错，也只改本地代码，把原因和服务器上要由用户自己敲的命令告诉用户。

写的时候要按线上会怎样来写：外网是 `http://120.76.58.132:8088`，宝塔反代的 Host 不带端口，进程是 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`。缺导入、反代丢端口、Cookie 路径，都会在线上直接坏掉。

改完停在本地。上传和重启线上由用户自己来。
