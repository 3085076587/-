---
name: local-server
description: >-
  改完或写完本地代码后不要自己启动或重启服务，把启动命令交给用户。Use when finishing
  a code change, starting or restarting the local API, uvicorn, or when the user
  says 启动服务、重启本地服务器.
---

# 本地服务用户自己启动

写完或改完代码后，不要启动服务，不要重启本地服务器。用户自己来。

结束后只输出这一条启动命令：

```
D:\fastapi-project\Ad_Helper\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```
