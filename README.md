# KitPrep 中央厨房 BOM 备料

按菜品 BOM 展开订单行、合并同原料需求，对照库存计算缺料并生成备料单。

技术栈：Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL / Vue 3 / TypeScript / Vite

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:5000 |
| API | http://localhost:10100 |
| API 文档 | http://localhost:10100/docs |
| Postgres | localhost:5451 |

健康检查：`GET http://localhost:10100/api/health`

## 使用说明

1. 在「菜品」「BOM」维护中央厨房出品与用料树。
2. 在「订单」「库存」确认当日需求与现有库存。
3. 打开「备料单」展开合并原料需求。
4. 在「缺料」查看 need − stock 为正的原料。

## 两店合单备料

在「备料单」顶栏点选两家门店订单后点「两店合单备料」，或调用
`POST /api/prep/merge`（body：`{"order_ids": [id1, id2]}`，也支持 query 参数）。

- 两店备料行、缺料贴、统计必须对成加总；对不上整次失败回滚，两边都不落新行。
- 空门店名单、只给一家、重复给同一单、订单不存在一律拒绝，已有单不动。
- 每店的备料行只记在自己订单编码下；第三家门店的已有单不受合单影响。
- `GET /api/prep/merge/shortages?order_ids=id1,id2` 只读查看两店加总缺料贴，不落库。

## 开发与测试

```bash
docker compose exec api pytest -q
```
