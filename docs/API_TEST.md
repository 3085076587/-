# 联调请求示例

先启动：

```powershell
cd D:\fastapi-project\Ad_Helper
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Swagger：http://127.0.0.1:8001/docs

## 1. 授权回调（浏览器）

打开巨量应用「授权 URL」。成功后会回调：

`https://chuanqian-test.free.idcfengye.com/api/oauth/callback?auth_code=...&state=...`

本地会自动换 token 入库，并后台拉 agent_id。

## 2. 手动保存 token

```bash
curl -X POST http://127.0.0.1:8001/api/oauth/save-token \
  -H "Content-Type: application/json" \
  -d "{\"access_token\":\"xxx\",\"refresh_token\":\"yyy\",\"expires_in\":86400,\"refresh_token_expires_in\":2592000}"
```

## 3. 拉取 agent_id

```bash
curl http://127.0.0.1:8001/api/oauth/fetch-agent-id
```

## 4. 同步全部子账户

```bash
curl http://127.0.0.1:8001/api/advertiser/sync-all
```

## 5. 同步某个子账户计划

```bash
curl "http://127.0.0.1:8001/api/plan/sync?advertiser_id=1234567890"
```

## 6. 同步报表

```bash
curl "http://127.0.0.1:8001/api/plan/report/sync?advertiser_id=1234567890&start_date=2026-10-01&end_date=2026-10-07"
```

## 7. 修改 ROI2（唯一写操作）

```bash
curl -X POST http://127.0.0.1:8001/api/plan/update-roi \
  -H "Content-Type: application/json" \
  -d "{\"advertiser_id\":1234567890,\"ad_id\":9876543210,\"roi2_goal\":2.5}"
```

## 8. 刷新 token

```bash
curl http://127.0.0.1:8001/api/token/refresh
```
