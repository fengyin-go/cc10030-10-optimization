# 航次编排协作台

航运调度员、船长和港口代理共同编排航线、分配船舶、登记货物并跟踪每个航次的装载与到港状态。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 船舶档案 | `vessel` | 船舶 | 船名、总吨位、载重吨 |
| 航线编排 | `route` | 航线 | 起运港、目的港、经停港口 |
| 航次编排 | `voyage` | 航次 | 船舶编号、航线编号、计划离港 |
| 货物登记 | `cargo` | 货物 | 货物品名、托运方、件数 |
| 船员排班 | `crew_shift` | 排班单 | 船员姓名、岗位、上船日期 |
| 燃料记录 | `fuel_log` | 燃料记录 | 船舶编号、加油港口、燃料类型 |
| 港口挂靠 | `port_call` | 挂靠记录 | 航次编号、挂靠港口、预计到港 |
| 箱号登记 | `container` | 集装箱 | 箱号、箱型尺寸、封条编号 |
| 气象预警 | `weather_alert` | 预警通知 | 预警海域、预警类型、预警等级 |
| 事故记录 | `accident_log` | 事故记录 | 发生时间、发生海域、涉事船舶 |
| 船舶检验 | `inspection` | 检验记录 | 船舶编号、检验类型、检验机构 |
| 通关申报 | `clearance` | 申报单 | 航次编号、申报类型、申报港口 |
| 租船合约 | `charter` | 合约单 | 租用船舶、承租方、日租金 |
| 泊位安排 | `berth` | 泊位安排 | 港口名称、泊位编号、靠泊船舶 |
| 舱单编制 | `manifest` | 舱单 | 航次编号、货物清单、托运方汇总 |
| 航海日志 | `voyage_log` | 日志条目 | 航次编号、记录时刻、船位经纬度 |
| 船舶保养 | `maintenance` | 保养记录 | 船舶编号、保养项目、保养周期 |
| 船员证书 | `crew_cert` | 证书记录 | 船员姓名、证书类型、证书编号 |
| 调度指令 | `dispatch_note` | 指令单 | 接收船舶、指令内容、发出时间 |
| 费率核算 | `tariff` | 费率单 | 服务项目、计费单位、标准费率 |
| 装载额度回执 | `load_quota` | 装载额度回执 | 船舶编号、航次编号、货物编号、预留吨数 |

## 重叠装载时段与装载额度回执

同一艘船被调度给多个航次时，载重阈值按**相交装载时段**核算（依据航次的
计划离港/计划到港），不再只看单航次：

- 货代确认装载前先申请「装载额度回执」，回执同时关联船舶、航次和货物，
  签发时即按预留吨数占用船舶在相交时段内的载重额度；
- 相交时段内 `待确认 + 已占用` 回执合计超过船舶载重吨时，新回执被拒绝
  （例如 100 吨船在重合窗口已占 60 吨，再来 50 吨会按 110 吨拦截）；
- 货物登记「确认装载」必须消费有效回执，同一张回执重复确认只占一次（幂等）；
- 「确认卸货」把回执置为已释放，吨位可让给不重合的下一趟航次；
- 航次「调整船舶」时，旧船上尚未实际装载的待确认回执一律撤下（已撤销），
  用旧船凭证确认同一张票会被拒绝；旧船释放出的额度仍可受理后续作业；
- 货物列表补注「配载状态/配载说明」区分等待配载与已能履约，
  `GET /api/cargo/{id}/readiness` 可查剩余额度和最早释放时间。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：应用开发
子方向：web前端/全栈开发
