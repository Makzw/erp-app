# PMC（MPS 计划）计算逻辑说明

> 适用：`/home/Mak/erp-app`（FastAPI + 单页 SPA，端口 8001，库 C041/T041）
> 对应代码：`main.py` 的「PMC 生产计划」段（`_PMC_POS_FILTER` … `pmc_adjust_stock`）、`static/index.html` 的 `renderPMCList/pmcShowSummary/renderPMCTree`
> 最后核对：2026-09-29（BOM 基数/单位修复后 `2a52ceb` 附近）

---

## 0. 一句话

PMC 干的事：**从待排产的销售订单行出发，按 BOM 一层层展开出全部自製件/原材料的需求，跟「库存 + 在途采购 + 在单请购」比，算出缺多少**；单张单看「本单缺口」，整个车间看「品号净缺口（可以直接拿去下单的数）」。

---

## 1. 界面入口 ↔ 接口对照

| 界面动作 | 函数 | 接口 | 写库 |
|---|---|---|---|
| PMC tab 列出待分析单 | `loadPMC / renderPMCList` | `GET /api/pmc/pos_unanalyzed` | 否 |
| 点一张卡片 → BOM 树 | `pmcAnalyzeRow → renderPMCTree` | `GET /api/pmc/preview_mps` | 否 |
| 勾多张 →「批量预览」 | `pmcPreviewBatch` | 前端循环调 `preview_mps` 后合并渲染 | 否 |
| 「品号汇总」按钮 | `pmcShowSummary` | `GET /api/pmc/prd_summary` | 否（后台线程） |
| 树里点某仓的「调整」 | `openPmcAdjust / doPmcAdjust` | `POST /api/pmc/adjust_stock` | **是**（IC 13/23） |
| 「生成 MPS」 | `pmcSubmit` | `POST /api/pmc/generate_mps` | **是**（MPS 表） |

> 另有 `POST /api/pmc/preview_generate` + `POST /api/pmc/generate`（生成 MO 工单 + QD 请购单）已实现，但**当前界面没有挂按钮**。

---

## 2. 数据来源

| 数据 | 来源 | 说明 |
|---|---|---|
| 销售订单行 | `VW_POS`（`OS_NO LIKE 'SO%'`） | 订单量 `QTY`、已出 `QTYPS`、审核人 `CHK_MAN`、指令单号、`EST_DD`、`MP` |
| 采购行 | `VW_POS`（`OS_ID='PO'`） | 采购未回量 = `QTY − ISNULL(PSQTY,0)` |
| 请购行 | `QTS`（`QT_ID='QD'`） | 在单请购量 = `SUM(QTY)` |
| 库存 | `VW_STOCK_DETAIL2`（逐仓） | `QTY_WH / QTY_ON_RSV / QTY_ON_PRC / QTY_ON_INS / QTY_ON_SCR / QTY_ON_WAY / QTY_ON_ODR` |
| 仓库分类 | `MY_WH.ATTRIB` | `5` 车间仓、`6` 外发仓 = **生产仓**；其余 = **原材料仓** |
| BOM | `BOM` + `PRDT` | `LEV=0` 是成品根，`LEV=1` 是直接子件（靠 `UPGUID=根GUID`） |

**BOM 配比的唯一口径**（2026-09-29 用 `BOM_2026.9.24_.xlsx` 修完 6,049 行后定稿）：

```
配比 bom_ratio = BOM.QTY ÷ BOM.QTY_BAS      # 用量 ÷ 基数
子件需求量      = 母件数量 × bom_ratio
```

---

## 3. 第一步：哪些单进「待分析」列表（`_PMC_POS_FILTER`）

```sql
MP = 0 AND USABLE = 1 AND OS_NO LIKE 'SO%'
  AND ISNULL(CHK_MAN,'') <> ''                                   -- 已审核（审核人）
  AND ISNULL(QTY,0) - ISNULL(QTYPS,0) > 0                        -- 销售未出 > 0
  AND ISNUMERIC(指令单号) = 1 AND CAST(指令单号 AS INT) > 7721     -- 老单不排产
```

- **已审核看 `CHK_MAN`**；`APP_ID / APP_MAN / APP_DD` 是「核准」（后一步，默认 0）—— 拿 `APP_ID=1` 当已审核是错的（老代码就这么错，把未审核单也放进来）。
- 空的/非数字指令单号（含 `TEST01`）一律不要；`9999` 是测试单但 `>7721`，**会留下**。
- SQL 里是 `TOP 200`：真积压超 200 会静默截断。
- 带搜索词 `q` 时，常量里的字面 `%` 要写成 `%%`（pymssql 会做 %-插值），所以代码用 `_PMC_POS_FILTER.replace('%','%%')` —— 两处共用同一个常量。
- 实测（2026-09-29）：旧口径 77 行（全部未审核）→ 新口径 **8 行**。

---

## 4. 第二步：需求基准 = 「销售未出」

`_so_line_info(so_no_itm)` → `(销售未出, 指令单号)`：

```
销售未出 = POS.QTY − POS.QTYPS          # 不是订单原始数量：已部分出货的只算未出
指令单号  = 用来把池子（在途/在单请购）挂到本单
```

- 用订单原始量会把「已出货的部分」重复排产。
- 传进来的 `qty`（前端卡片上的数量）只作为**查不到该 SO 行时的兜底**。
- 指令单号同时决定第 7 步的挂单。

---

## 5. 第三步：BOM 展开（`_mps_bom_tree`）

```
_mps_bom_tree(fg_no, conn, qty) →
  [(prd_no, prd_name, demand_qty, knd, parent, depth, bom_ratio), ...]
```

- 找 `BOM.PRD_NO = fg_no AND LEV=0` 的根 GUID → 取 `UPGUID = 根GUID AND LEV=1 AND ISNULL(删除,0)=0` 的直接子件，按 `IDX` 排序。
- `KND=4`（原材料）到此为止；`KND=2/3`（组件/中间件）**先记自己，再递归展开**（所以树里中间件和它的子件都在）。
- 递归上限 `depth >= 8`，并且用 `_seen` 防环（同一个品号在一棵子树里只展开一次）。
- 需求累乘：下一层的 `qty` 传的是上一层的 `demand_qty`，`bom_ratio` 只保存不预先乘 —— 分配阶段（第 8 步）才用。

---

## 6. 第四步：库存分仓（`_v2_stock` / `_v2_stock_detail`）

对每个品号，把 `VW_STOCK_DETAIL2` 的逐仓行按仓分类累加：

```
可用量 qty_av = max(0, QTY_WH − QTY_ON_RSV − QTY_ON_PRC − QTY_ON_INS − QTY_ON_SCR)
生产仓 qty    = Σ(ATTRIB ∈ {5,6} 的仓)      prod_qty / prod_av
原材料仓 qty  = Σ(其余仓)                    mat_qty  / mat_av
```

> 口径由 MAK 定：**生产仓 = ATTRIB 5+6 之和；原材料仓 = 其他所有仓位之和**（含成品仓/半成品仓/通用库位，列名仍叫「原材料仓」）。

⚠ **`VW_STOCK_DETAIL2` 里没有「请购在单」这一列**，两列很容易搞混：

| 视图列 | 真实含义 | 源头 |
|---|---|---|
| `QTY_ON_WAY` | **采购未回 + 请购在单**（两者之和） | `VW_PO_QTY`` = 采购单未回 ∪ `QTS(QT_ID='QD')` 数量 |
| `QTY_ON_ODR` | **销售未出货量**（不是请购！） | `VW_SO_QTY` = `SUM(POS.QTY − POS.SAQTY)` |

拿 `QTY_ON_ODR` 当供给去扣，等于把需求又减一遍，整棵树会被扣成全 0（踩过：演示单需求 1,200、在单请购 1,200 → 缺口 −60、子件全 0）。

---

## 7. 第五步：池子按单挂（`_v2_odr_split`）

在途 / 在单请购在库里是**品号级池子**，逐单算时不能整池扣给每一张单。所以按「这笔料是给哪张单下的」挂：

```
采购未回（VW_POS OS_ID='PO'，QTY > PSQTY）：
    池子总量 po_all = 该品号全部未回量
    挂本单   po     = 其中 [指令单号] LIKE %本单指令单号%

在单请购（QTS QT_ID='QD'）：
    池子总量 qts_all = 该品号全部在单请购
    挂本单   qts     = ( [指令单号] = 本单指令单号 )
                      OR ( [指令单号] 空白 且 [成品编号] = 本成品品号 )
                      OR ( SO_NO_ITM = 本 SO 行 )
```

- **优先级**：指令单号命中 → 挂本单；指令单号**空**才用「成品编号」兜底（成品编号只说明给哪个成品请的，同产品多张单区分不出来）；SO 行命中也算本单。
- **挂别的单、或挂不上的（纯库存份额、「X/库存」）→ 本单不扣**（不分摊）。
- 返回里 `po_all/qts_all` 只用于屏上 tooltip（「池 xxx，挂的是别的单 → 本单不分摊」）。
- 实测：采购未回 230 行里 204 行带指令单号；在单请购 68 行 68 行带指令单号（65 行还带成品编号）。50 张单里**只有 1 张**真挂上了本单的料。

---

## 8. 第六步：需求分配 + 缺口（`allocate` / `make_row`）

### 8.1 自顶向下按「BOM 边」分配

```
父件缺口 = max(0, 父件需求 − 父件本单专属供给)
子件需求 = 父件缺口 × 该边的 bom_ratio
起点    = allocate(成品品号, 销售未出, 成品自身的本单专属供给)
```

三个不能改的点：

1. **按边分配，不按品号索引**。同一料号会挂在多个母件下（50 单里有 54 个），用品号→行下标字典会让后一个覆盖前一个，前一行留着错值。
2. **顶层要传成品自己的供给**，不能传 0（传 0 时成品仓的货不抵，整单子件需求虚高）。
3. **公共库存不参与分配**（不分摊）。子件需求是「毛需求」，公共库存只在第 9 步品号层扣一次。

### 8.2 每行的数（`preview_mps` 返回 / 树上每列）

| 字段 | 公式 | 含义 |
|---|---|---|
| `real_demand` | 父件缺口 × 配比（成品行 = 销售未出） | **需求** |
| `so_remain` | `QTY − QTYPS`（只有成品行有值） | **销售未出** |
| `mat_qty` | Σ 原材料仓库存 | **原材料仓** |
| `prod_qty` | Σ 生产仓库存 | **生产仓** |
| `qty_on_way` | 挂本单的采购未回 `po` | **在途采购** |
| `qty_on_odr` | 挂本单的在单请购 `qts` | **在单请购** |
| `total_avail` | `qty_on_way + qty_on_odr` | **合计**（本单专属供给，不含公共库存） |
| `gap` | `real_demand − total_avail` | **缺口**（不含公共库存的本单缺口） |
| `prd_net / prd_need / prd_stock / prd_orders` | 来自品号汇总缓存（热了才有） | **品号净缺** 徽标 |

> `total_stock`（`mat_qty + prod_qty`）仍返回，但**「合计」列显示的是 `total_avail`**，因为公共库存不分摊。

---

## 9. 第七步：品号级汇总 = 净缺口（`_calc_prd_summary`）

```
毛需求合计 need = Σ(该品号在所有待分析单里的 real_demand)
挂本单合计 own  = Σ(该品号在所有待分析单里的 total_avail)
库存     stock  = 原材料仓 + 生产仓（现存）
单层缺口 row_gap = need − own
净缺口   net     = max(0, row_gap − stock)        # 库存只扣这一次
```

- 跑一遍要展开**所有**待分析单的 BOM（几十秒）→ 后台线程算 + 内存缓存 `_PRD_SUM`（TTL 600 秒）。
- **单飞**：`busy` 或 30 秒内刚踢过就不重复开线程（否则每次轮询都开一个新线程一起算，永远算不完）。
- 前端：列表工具栏「品号汇总」→ 表格（品号/品名/需求合计/库存/净缺口/单数），按净缺口降序，`net ≤ 0` 的行半透明；`pmc_preview_mps` 在缓存热时给每行挂 `prd_net`，树里显示成「品号净缺 X」小字。
- 为什么需要它：**采购是按品号下单的**，同一品号被多张单用到时，单层缺口会各扣一遍（单层的数只能看「本单要多少」，不能拿去下单）。
- 实测（8 张待分析单）：121 个品号；单层缺口加总 659,302 → 净缺口 433,715（库存顶掉约 22.5 万）；`P20365-02-07` 因库存 **−113,546**（负数）反而净缺 120,346。

---

## 10. 数值走一遍（SO26090029004 / 0067(605569/851391)，2,000 件）

```
L0 0067(605569/851391)   需求 2,000                     ← 销售未出
L1 P20610-01-ZN1         需求 2,000
L2 P20610-01-02-ZN1      需求 2,000
L3 P20610-01-02          需求 2,000
L4 RM01C2362.0           需求   164 = 2,000 × 0.082 ÷ 1  ← 基数修好后才对（原来是 2,000）
L2 P20610-01-03-ZN1      需求 4,000  → L3 需求 8,000（配比 2）
L2 SC0037-ZN             需求 16,000（配比 8）
```

---

## 11. 写库路径

### 11.1 生成 MPS（`POST /api/pmc/generate_mps`）

- 合并规则：**同成品品号 + 同销售订单**才合并数量。
- 生成单号 `MP{YYMM}{4位流水}`（`LEN(MPS_NO)=10`），单头取第一条 POS 的客户/价格/交期。
- 每个成品一行 ITM，其 BOM 子件顺序接在后面；插入 `MPS` 表 24 列：
  `MPS_NO, MPS_DD, USR('Hermes'), USABLE=1, ITM, CUS_NO, CUS_NAME, FG_NO_SO, SO_NO_ITM, REF_ITM, PRD_NO, PRD_NAME, SPC, UT, QTY, WH, WH_NAME, QTY_WH, QTY_AV, 指令单号, EST_DD, UP, BOM=1, STA_DD`。
- `QTY_WH/QTY_AV`：成品行取生产仓，子件行取原材料仓。

### 11.2 库存调整（`POST /api/pmc/adjust_stock`）

```
diff = 输入数量 − 当前库存
diff > 0 → IC KND=13（其他入库），diff < 0 → IC KND=23（其他出库），数量取 |diff|
输入 == 当前 → 直接返回「库存相同，无需调整」（不写库）
```
- IC_NO = `IC{YYMM}{4位流水}`（当天最大 +1，`LEN=10`）；IC.REM 记「PMC调整 / 运输工具:品号 名称 数量」。
- 前端会拦：仓码必须在该料的仓库列表里；空/非数字拦下，显式 `0` 放行（= 清仓）。

---

## 12. 已知坑 / 历史修正（改这块前先看）

| # | 坑 | 现状 |
|---|---|---|
| 1 | 需求基准用订单原始量（已出货部分重复排产） | 已改 `QTY − QTYPS` |
| 2 | 「在单请购」取 `QTY_ON_ODR`（其实是销售未出） | 已改 `_v2_odr_split` |
| 3 | 按品号索引分配 BOM 需求（同料多母件时覆盖） | 已改按边分配 |
| 4 | 顶层 `allocate(..., 0)` 导致整单虚高 | 已改传成品自身供给 |
| 5 | 池子整池扣给每张单 / 公共库存分摊 | 已改「按单挂 + 库存不分摊」 |
| 6 | 已审核判定用 `APP_ID=1` | 已改 `CHK_MAN` |
| 7 | 品号汇总轮询每次开新线程 | 已加单飞 |
| 8 | BOM 基数被写成用量（`QTY_BAS = QTY`）→ 配比恒为 1 | 2026-09-29 按 `BOM_2026.9.24_.xlsx` 修 6,049 行 |
| 9 | **`generate_mps` 里 `for c_prd, c_name, c_qty, c_knd in comp_rows` 是 4 元解包，而 `_mps_bom_tree` 返回 7 元 → 点「生成 MPS」必 500（`ValueError: too many values to unpack`）。** | ⚠ **未修** |
| 10 | 库里有 89 行 `QTY_WH < 0`（最大 −113,546），会把某些品号净缺口撑大 | ERP 里的真数据，不是算错 |
| 11 | 待分析单 SQL 仍是 `TOP 200` | 积压超 200 会静默截断 |
| 12 | `9999` 测试单（`SO26090024`）满足筛选条件会进列表 | 待 MAK 定去留 |
| 13 | 品号汇总后台线程正在算（`_calc_prd_summary` 里逐单 `asyncio.run(preview_mps)`）时，并发/批量打 `preview_mps` 偶发 500（2026-09-29 实测一次，重跑即过） | 别在汇总计算中批量压接口；要批量核数就等汇总算完 |

---

## 13. 自检（改完必跑，只读）

```bash
cd /home/Mak/erp-app && env -u PYTHONPATH /usr/bin/python3 pmc_formula_check.py
```

断言（逐行）：
- `合计 == 在途采购 + 在单请购`（公共库存不进合计）
- `挂本单的在途/在单请购 ≤ 该品号池子总量`
- `缺口 = 需求 − 合计`
- 成品行 `需求 = 销售未出`
- `需求 = 父件缺口 × 配比`（脚本里有一份独立的按边参考实现，不看 `main.py` 的分配代码）
- 输出 `PASS / N 单 M 行 0 失败` 才算过；改前跑它可直接看出哪几行错，是最省事的回归网。
