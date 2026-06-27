# 报告导出改进：股票名称 + K线止盈止损标线图（纯结论报告）

- 日期：2026-06-27
- 范围：分析报告导出（`导出报告` 按钮入口，`_on_export_report`）
- 目标用户反馈：
  1. 报告光有股票代码没有名称，看着不方便 → 报告头加股票名称。
  2. 想把 K 线柱的止盈止损标线展示在报告里 → 报告嵌入带标线的 K 线图。
  3. 报告里的文字依据是当初 debug 用的，现在只要结论报告 → 删除全部文字依据。

## 1. 背景

当前导出流程（`pa_agent/records/report_exporter.py::export_report_md` →
`pa_agent/gui/main_window.py::_on_export_report`）只产出一份纯 Markdown 文本报告，
共 7 节（结论 / 置信度 / 市场诊断依据 / 决策依据 / 风险与观察 / 推演结论 / 附录），
报告头只有 `symbol`（如 `002272`），没有股票名称，也没有 K 线图。

项目里已有一个成熟的 matplotlib K 线渲染器
`pa_agent/records/trade_logger.py::_render_chart`，能画 K 线 + EMA20 + Entry/TP1/TP2/SL
水平虚线 + 方向箭头 + 订单类型徽章 + 置信度徽章，用于 `trade_records/<symbol>_<tf>.csv`
的配套图。但它目前**无法生成**，因为 `matplotlib` 不在依赖里（日志反复出现
`matplotlib not installed; skipping chart generation`），而且它**不画支撑/阻力位**，
也没有被报告导出流程调用。

主界面图（`ChartWidget`）实际显示的标线有两类：
- 决策线：Entry / TP1 / TP2 / SL（`set_decision`）。
- 结构线：支撑位（绿）/ 阻力位（橙）（`set_support_resistance`，源自 stage1 的
  `support_levels` / `resistance_levels`，经 `gui/support_resistance.py` 解析）。

用户要的「图里都显示出来」即主界面图这一整套：**决策线 + 支撑/阻力位**。

## 2. 目标

1. **股票名称**：报告头把代码翻译成名称（`002272` → `川润股份`），查不到则只显示代码。
   不改记录结构（`RecordMeta` 不动），导出时实时查询 + 进程级缓存，老记录也享受。
2. **K 线图嵌入**：报告嵌入 PNG，图中含 K 线 + EMA20 + Entry/TP1/TP2/SL 决策虚线 +
   支撑/阻力位结构虚线 + 方向箭头 + 订单类型/置信度徽章。即完整复刻主界面标线。
3. **纯结论报告**：删除「置信度 / 市场诊断依据 / 决策依据 / 风险与观察」四节文字依据，
   报告精简为结论表（含一句决策理由）+ K 线图 + 推演结论 + 附录。

## 3. 报告最终结构

````
# {名称}（{symbol}）{timeframe} 分析报告       ← 名称优先，查不到则「{symbol}」
> 生成时间：… · 决策立场：… · K线数：… · 当前价：… · 模型：…

## 一、分析结论
| 项目 | 内容 |                       ← 有单：完整参数表
| 操作类型 | 限价单 |
| 方向 | 做多 |
| 当前价 | … |
| 入场价 | … |
| 止盈价(TP1) | … (+x.x%) |
| 止盈价(TP2) | … (+x.x%) |
| 止损价 | … (+x.x%) |
| 盈亏比 | 风险 … / 回报 … / RR … |
**决策理由**：{一句话总结}                ← 保留（属结论非明细依据）

![K线分析图]({同名}.png)                  ← 新增，含全部标线

## 二、推演结论
### 下一根K线 …
### 下一市场周期 …

## 三、附录
- Token 用量：…
- 策略文件：…
- 经验库条目：… 条
- ⚠️ 异常：…（仅当有异常）
````

不下单时：第一节只显示 `**操作类型**：不下单` + 决策理由（若有），不渲染空表，
也不嵌入决策虚线（图照常出，只有 K 线 + 支阻位）。

## 4. 改动清单

### 4.1 新增 `pa_agent/records/symbol_name_resolver.py`

`resolve_stock_name(symbol: str) -> str`：

- 进程级缓存：`dict[symbol, (name, expire_ts)]`，TTL 10 分钟，命中直接返回。
- **A 股**（6 位数字，首位 `0/3/6/8/4`）：用 akshare 单只个股接口查名称。
  首选轻量接口（如 `stock_individual_info_em`）；全市场 `stock_zh_a_spot_em` 太慢（70s+）禁用。
  取到的「股票简称」字段作为名称。
- **港股 / 美股 / 指数 / 外汇 / 黄金**（非纯数字 symbol）：调用新增的
  `tv_symbol_lookup.lookup_name_by_symbol(symbol)` 做本地别名表反查。
- 全程 `try/except`：akshare 网络失败、接口字段变动、任何异常 → 返回 `""`。
  名称是「锦上添花」，**绝不让导出因名称查询失败而中断**。
- 无副作用：不在 GUI 线程长时阻塞；akshare 调用自带项目里已有的节流约定
  （`akshare_source.py` 里 East Money 限频）。

### 4.2 修改 `pa_agent/records/report_exporter.py`

- 签名改为 `export_report_md(record, *, chart_png_name: str | None = None) -> str`。
- 报告头：`# {title} {timeframe} 分析报告`，其中
  `title = f"{name}（{symbol}）" if name else symbol`。
  名称由调用方（`_on_export_report`）查好后传入；为保持函数自洽与可单测，
  导出器内部不再二次查询，名称通过新增的「可选入参 `stock_name: str | None = None`」
  传入。最终签名：`export_report_md(record, *, stock_name=None, chart_png_name=None)`。
- **删除**：原「二、置信度」「三、市场诊断依据」「四、决策依据」「五、风险与观察」
  四节的全部渲染代码及其辅助函数（`_bullet_list` 若不再被引用则一并删除，
  仍被引用则保留）。
- **保留**：第一节结论表 + 决策理由一行 + 决策终局（若有）；
  原第六节推演结论改编号为「二」；原第七节附录改编号为「三」。
- 图的引用：第一节结论表（及决策理由）之后插入
  `![K线分析图]({chart_png_name})` + 空行，仅当 `chart_png_name` 非空时插入。
- 容错不变：所有字段缺失仍渲染为 `—`，不下单仍走简版结论。

### 4.3 修改 `pa_agent/records/trade_logger.py::_render_chart`

- 新增可选参数 `support_resistance: list[tuple[float, str]] | None = None`，
  元素为 `(price, kind)`，`kind ∈ {"support", "resistance"}`。
- 在现有 Entry/TP1/TP2/SL 虚线绘制逻辑之后，追加绘制支阻位虚线：
  - 支撑：绿色 `#4ade80`，标签前缀「支撑」。
  - 阻力：橙色 `#fb923c`，标签前缀「阻力」。
  - 画法复用现有 `axhline + 右侧文本标签` 的样式（虚线、右侧锚点、半透明底色），
    与决策虚线视觉一致但颜色区分。
- 支阻位价格可能重复或过密：仅画前 N（如 3）个支撑 + 前 3 个阻力，避免图面拥挤。
- 现有调用点（`_save_trade_record_impl`）不受影响：不传 `support_resistance` 时行为不变
  （向后兼容，`trade_records` 的 CSV 配套图不画支阻位，保持现状）。

### 4.4 修改 `pa_agent/gui/main_window.py::_on_export_report`

- 调 `resolve_stock_name(symbol)` 查名称（best-effort，失败则空）。
- 从 `record.kline_data`（list[dict]）构造 bars；用项目现有指标计算重算 EMA20
  （复用 `indicators` 模块的 EMA 函数；算不出则 EMA20 列为空，图只画 K 线）。
- 从 `record.stage1_diagnosis` 提取 `support_levels` / `resistance_levels`，
  用 `gui/support_resistance.py::levels_from_stage1_diagnosis` 解析成
  `StructureLevel` 列表，再映射成 `(price, kind)` 传给 `_render_chart`。
- 调 `_render_chart(...)` 生成 PNG，写到与用户选择的 `.md` 同目录的**同名 `.png**`。
- 调 `export_report_md(record, stock_name=name, chart_png_name=png文件名)` 生成 md，
  md 里写**相对引用**（只写文件名，因为 png 与 md 同目录）。
- 保存 md。文件名沿用现有 `symbol_timeframe_ts.md` 规则。
- **降级**：`matplotlib` 未装 → 跳过画图，状态栏提示
  `未安装 matplotlib，已跳过K线图`，md 正常导出（不插图引用）。
  渲染 PNG 抛异常 → 同样降级，日志记录，不阻断 md 导出。
- 不下单时仍可出图（K 线 + 支阻位，无决策虚线）。

### 4.5 修改 `pa_agent/data/tv_symbol_lookup.py`

- 新增 `lookup_name_by_symbol(symbol: str) -> str | None`：遍历 `_all_aliases()`，
  匹配 `symbol`（忽略大小写、去前导零港股），返回对应的原始 key（中文名/英文名）。
  用于港股/美股代码反查名称。

### 4.6 修改 `pyproject.toml`

- `dependencies` 增加 `matplotlib>=3.8`。
  这同时修复 `trade_logger` 一直无法生成 CSV 配套图的遗留问题。

## 5. 容错矩阵

| 情况 | 名称 | 图 | md |
|------|------|----|----|
| 正常 | 显示 | 嵌入 | 含图引用 |
| matplotlib 未装 | 显示 | 不出 | 不含图引用，状态栏提示 |
| 不下单 | 显示 | K线+支阻位（无决策虚线） | 简版结论 |
| 名称查不到 | 只显示代码 | 正常 | 正常 |
| akshare 网络失败 | 只显示代码 | 正常 | 正常 |
| EMA 重算失败 | 正常 | 只画K线+标线（无EMA） | 正常 |
| kline_data 为空 | 正常 | 不出图 | 不含图引用 |

铁律：**名称查询与画图失败都不得阻断 md 导出**；md 导出失败才弹错（现有行为）。

## 6. 测试

- `tests/unit/test_symbol_name_resolver.py`（新增）：
  - mock akshare，验证 A 股代码 → 名称、查不到 → `""`、网络异常 → `""`、缓存命中。
  - 港股代码经 `lookup_name_by_symbol` 反查命中。
- `tests/unit/test_report_exporter.py`（新增或补）：
  - 报告头有名称：`川润股份（002272）`；无名称：`002272`。
  - 传 `chart_png_name` 时含 `![K线分析图](...)`，不传时不含。
  - 删除的依据节不再出现（断言无「置信度」「决策依据」「市场诊断依据」标题）。
  - 保留：结论表、决策理由、推演结论、附录。
- `tests/unit/test_trade_metrics_chart_lines.py` 或新增 chart 渲染测试：
  - 传 `support_resistance` 后图中出现支撑/阻力虚线（可用 matplotlib mock 或检查
    `ax.axhline` 调用次数/颜色）。

## 7. 不做（YAGNI）

- 不改 `RecordMeta` schema，不新增持久化字段（名称实时查）。
- 不导出 HTML / base64 内嵌（用户已选 md + png 两文件）。
- 不动 `trade_records` CSV 流程的现有行为（仅让 `_render_chart` 多一个可选参数）。
- 不改主界面 `ChartWidget` 显示逻辑。
- 不做历史报告回填名称。
