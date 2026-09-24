# erp-app

中山安而固 ERP 管理系统（手机端）

## 技术栈

- **后端**：FastAPI + Pymssql（C041 数据库）
- **前端**：单页应用（index.html）
- **端口**：8001

## 启动

```bash
cd /home/Mak/erp-app
python3.11 -m uvicorn main:app --port 8001 --host 0.0.0.0
```

## API

- `/api/smo?db=c041` — 派工单列表
- `/api/pmc/pos_unanalyzed` — PMC 待分析订单
- `/api/pmc/preview_mps` — BOM 预览
- `/api/completion/list` — 完工列表
- `/api/transfer/list` — 调拨列表
- `/api/stock/list` — 库存列表
- `/api/qts` — 采购未回
- `/api/pos_unreceived` — 采购未回
