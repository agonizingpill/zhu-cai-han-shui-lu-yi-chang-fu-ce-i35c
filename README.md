# 竹材含水率异常复测

面向安吉青篁竹材集散中心质量组的入库、含水率异常隔离、窑干复测与出库放行工作台。系统使用多点采样建立批次湿度画像，通过 `StandardScaler + OneClassSVM` 识别偏离正常基线的批次，并把异常批次带入可追溯的窑位排程和主管放行流程。

## 真实场景

模拟客户日均 30–50 批毛竹入库。仓管在纸质批次卡写产地和采伐日，检验员使用 MT-07 手持仪在 Excel 中登记 4–6 个测点，窑炉班组通过微信群协调窑位。痛点是测点缺失后仍可能被手工判为正常，异常批次隔离和窑位重排缺少统一证据，主管盖章时难以回看复测后的最后读数。

上游输入为入库过磅、产地/采伐日和 MT-07 测点记录。本系统替代手工湿度判断、异常隔离和窑位沟通。下游为窑炉班组和出库主管，消费窑位排程、复测结论和出库放行记录。

## 功能模块

- 竹材入库 `BambooLot`：批次号、产地、采伐日、树种、毛重与目标含水率。
- 含水率采样 `MoistureSample`：每个点位只能录入一次，少于 4 点时禁止建模。
- 异常批次 `AnomalyLot`：持久化 OneClassSVM 判定、标准化距离、模型 checksum 和隔离状态。
- 窑干复测 `KilnRecheck`：记录窑位时间窗，冲突时创建 `RESCHEDULED` 版本并保留原排程。
- 出库放行 `ReleaseDecision`：只有 supervisor 能盖章，异常复测未闭环时后端拒绝放行。
- 初检判定 `InitialInspection`：每批可多次送检（人工登记 / 模型初检），有效结论以最后送检为准；晚到的判定按原入库位置参与重算。
- 产地预警 `OriginAlert`：同一产地按入库先后连续三批初检判异常即开一张预警单，每段只开一单。
- 预警更正 `OriginAlertCorrection`：已确认预警的依据被后续判定推翻或段发生变化时挂账，确认时的快照不改。

## 产地连续异常预警口径

- 序列：同一产地的批次按 `received_at`（入库先后，同刻按批次 id）排列；每批的当前结论取 `initial_inspections` 中最后送检的一条。
- 开单：序列中连续 3 批 `ABNORMAL` 构成一段，开一张 `OPEN` 预警单；段延伸（第 4、5 批连上）沿用原单，重算与重试不重复开单。
- 先等：未判批次截断"已确认连续"，既不计入也不永久打断；判定到达（含晚到）后按原入库位置整列重算。
- 撤回：`OPEN` 单的依据随重算实时更新；连续段不再成立时自动撤回并记录原因。
- 确认：supervisor 在总览页核对依据后确认，请求携带看到的 `basis_hash`；若确认时依据已变，后端返回 409 请其重新核对。
- 冻结与更正：`CONFIRMED` 单冻结当时的批次与判定；之后结论被推翻、段延伸/收缩或恢复一致，都各挂一条更正。
- 上报幂等：worker 的模型初检结论按 `analysis_job id` 幂等登记（`initial_inspections.job_id` 唯一），重试不重复记录、不重复开单。

## 预警相关接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/lots/{id}/inspections` | 登记初检判定（operator/supervisor），随后整产地重算 |
| GET | `/alerts` | 预警单列表（含依据与更正）+ 盯防清单 |
| POST | `/alerts/{id}/confirm` | 主管确认，body 带 `basis_hash`，依据已变返回 409 |
| POST | `/internal/inspections` | worker 上报模型初检结论，需 `X-Internal-Token` |

seed 预置三个演示场景：广西融安 3 连异常（一张待确认单）、湖南绥宁 2 连异常 + 1 批待判（盯防中）、江西宜春 3 连异常已确认后中间批被推翻（挂更正）。

## 算法口径

worker 从 Redis 队列读取分析任务，将每个批次的测点压缩为 `[均值, 标准差, 最小值, 最大值]`，用无异常单的已采样批次拟合 `StandardScaler`，再训练固定参数的 `OneClassSVM(kernel=rbf, gamma=scale, nu=0.2)`。当前批次标准化距离为 `||x_scaled||₂`，`prediction=-1` 或距离不小于 `2.25` 时进入异常隔离。固定种子为 `1075`。结果写入 `analysis_jobs` 和 `anomaly_lots`。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
```

访问 `http://localhost:18274`。容器内 seed 会自动幂等执行，也可运行 `docker compose exec backend python scripts/seed_demo.py` 复核。

## 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| operator | Bamboo#1075 | 录入、采样、送模型、排程、回写复测 |
| supervisor | Bamboo#1075 | 包含 operator 权限，可主管放行 |
| viewer | Bamboo#1075 | 查看批次、异常、放行记录 |

## 枚举位置

后端枚举在 `backend/src/constants/enums.py`，前端枚举在 `frontend/src/constants/enums.js`。具体状态前置条件在 `backend/src/main.py` 的入库、采样、分析、排程、复测和放行路由中。预警引擎为纯函数，位于 `backend/src/services/streaks.py`（单元测试 `backend/tests/test_streaks.py`，可用 `python3 backend/tests/test_streaks.py` 直接运行），落库与重算在 `backend/src/services/alerts.py`。

## 技术栈与目录

| 层 | 技术 |
|---|---|
| frontend | Nginx + 原生 HTML/CSS/ES modules |
| backend | Python 3.11 + FastAPI + psycopg2 |
| worker | Python 3.11 + Redis BRPOP + scikit-learn |
| db | PostgreSQL 16 |
| queue | Redis 7 |

领域代码位于 `backend/src/main.py`、`backend/src/services/moisture_ml.py`、`backend/src/models/schema.sql` 和 `frontend/src/app.js`。seed 位于 `backend/scripts/seed_demo.py`。

## License

Internal demonstration project, MIT-style use for evaluation.
