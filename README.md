# Markdown 引用闭包校验服务

基于 **FastAPI + SQLite** 的 Markdown 文档引用闭包校验 API。客户端提交一组带路径的
Markdown 文档，服务解析相对链接、标题锚点与代码块，建立跨文档引用图，检测断链、
锚点碰撞与循环引用，并返回按路径定位的诊断；修复预览接口只重写受影响的标题锚点
与引用目标，代码块与正文保持不变。

## 功能

- **文档导入**：批量提交 `{path, content}`，路径自动规范化并校验（拒绝绝对路径与
  `..` 逃逸）。
- **引用图构建**：解析行内链接、图片、引用式链接（仅统计被使用的定义）、ATX 标题
  （GitHub 风格锚点，支持 `{#custom}` 显式锚点与 CJK），跳过围栏/缩进代码块与行内
  代码中的内容。
- **诊断**（按 `path` + `line` 定位）：
  | 类型 | 级别 | 说明 |
  |---|---|---|
  | `broken_link` | error | 链接目标不在导入的文档集合中 |
  | `broken_anchor` | error | 目标文档存在，但 `#fragment` 无对应锚点 |
  | `anchor_collision` | warning/error | 多个标题共享同一锚点（歧义）或锚点重复定义 |
  | `cycle` | warning | 文档间循环引用（强连通分量，含自引用） |
- **校验任务**：校验在后台任务中执行，可查询任务状态与诊断（支持按类型/路径过滤）。
- **结果对比**：对两次 `done` 状态的校验，按稳定指纹（`type`、`severity`、
  `path`、`line`、`message`、`details`）计算新增 / 移除 / 未变化诊断，并报告
  两次导入之间文档的新增、删除与内容变化路径（`POST /api/validations/compare`）。
- **修复预览**：返回受影响文档的逐行变更与完整修复后内容，**不改写代码块与正文**：
  - 锚点碰撞 → 为重复标题补充显式锚点 `{#id}`（尽量保持原锚点值不变）；
  - 断锚 → 以最相似的目标锚点重写引用片段（相似度阈值 0.6）；
  - 断链 → 若集合中存在唯一同名文件，重写为正确的相对路径；
  - 循环引用 → 结构性问题，不自动修复，列入 `unfixed`。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build        # 构建并前台启动（-d 可后台运行）
# API: http://localhost:8000  (交互式文档: /docs，OpenAPI: /openapi.json)
```

镜像基于 `python:3.12-slim`，启动命令为
`uvicorn app.main:app --host 0.0.0.0 --port 8000`，数据库 schema 在容器启动时
自动初始化，无需手动建表。SQLite 数据库固定写入容器内 `/data/app.db`
（`DB_PATH=/data/app.db`），并通过命名卷 `mdlink-data` 持久化，重建容器后
导入与校验记录仍然保留。

常用操作：

```bash
docker compose logs -f api       # 跟踪服务日志
docker compose down              # 停止并删除容器（保留数据卷）
docker compose down -v           # 同时删除数据卷（清空全部历史数据）
curl localhost:8000/health       # => {"status":"ok"}
```

需要改用其他端口时，编辑 `docker-compose.yml` 中的端口映射（如
`"18000:8000"`）；也可以直接用 Docker 运行并自行挂载数据目录：

```bash
docker build -t mdlink-validator .
docker run -p 8000:8000 -v "$PWD/data:/data" mdlink-validator
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

数据库（SQLite）在启动时自动初始化，默认路径 `./data/app.db`，可用环境变量
`DB_PATH` 覆盖。

### 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/imports` | 导入文档集合 → `import_id` |
| `GET` | `/api/imports/{import_id}` | 查看导入批次与文档路径 |
| `GET` | `/api/imports/{import_id}/documents/{path}` | 读取单个文档 |
| `POST` | `/api/validations` | 对某导入批次发起校验（后台任务）→ `validation_id` |
| `GET` | `/api/validations` | 最近校验任务列表 |
| `GET` | `/api/validations/{validation_id}` | 任务状态 + 诊断（`?type=`、`?path=` 过滤） |
| `POST` | `/api/validations/compare` | 对比两次校验的诊断与文档差异（均须为 `done`） |
| `GET` | `/api/validations/{validation_id}/sarif` | SARIF 2.1.0 导出（任务须为 `done`） |
| `POST` | `/api/validations/{validation_id}/fix-preview` | 修复预览（任务须为 `done`） |

### 示例

```bash
# 1. 导入
curl -X POST localhost:8000/api/imports -H 'Content-Type: application/json' -d '{
  "documents": [
    {"path": "README.md", "content": "# 项目\n\n参见 [安装](docs/setup.md#安装)。\n"},
    {"path": "docs/setup.md", "content": "# 安装\n\n## 步骤\n\n## 步骤\n"}
  ]}'
# => {"import_id": "...", "doc_count": 2, ...}

# 2. 发起校验
curl -X POST localhost:8000/api/validations \
  -H 'Content-Type: application/json' -d '{"import_id": "<import_id>"}'
# => {"validation_id": "...", "status": "pending"}

# 3. 查询任务与诊断
curl localhost:8000/api/validations/<validation_id>
curl 'localhost:8000/api/validations/<validation_id>?type=broken_link&path=README.md'

# 4. 修复预览
curl -X POST localhost:8000/api/validations/<validation_id>/fix-preview

# 5. 对比两次校验（如文档修订前后各跑一次校验）
curl -X POST localhost:8000/api/validations/compare \
  -H 'Content-Type: application/json' \
  -d '{"base_validation_id": "<base_id>", "target_validation_id": "<target_id>"}'

# 6. SARIF 2.1.0 导出（Content-Type: application/sarif+json）
curl localhost:8000/api/validations/<validation_id>/sarif -o results.sarif
```

修复预览响应：

```json
{
  "fixed_documents": [
    {
      "path": "docs/setup.md",
      "changes": [
        {"line": 5, "kind": "heading-anchor",
         "before": "## 步骤", "after": "## 步骤  {#步骤-1}"}
      ],
      "fixed_content": "# 安装\n\n## 步骤\n\n## 步骤  {#步骤-1}\n"
    }
  ],
  "unfixed": [
    {"type": "cycle", "path": "README.md", "reason": "circular references require manual restructuring"}
  ],
  "stats": {"documents_rewritten": 1, "edits": 1, "unfixed": 1}
}
```

## 校验结果对比

`POST /api/validations/compare` 请求体：

```json
{"base_validation_id": "<base_id>", "target_validation_id": "<target_id>"}
```

两次校验都必须处于 `done` 状态。诊断按六元组稳定指纹
（`type`、`severity`、`path`、`line`、`message`、`details`）匹配，
`details` 采用键排序后的规范化 JSON 参与指纹（键顺序不同视为相同）；
匹配按多重集语义计数，重复出现的相同诊断（如循环中每个成员各一条）逐条对应。

响应中：

- `diagnostics.added`：target 相对 base 新出现的诊断；
- `diagnostics.removed`：target 相对 base 消失的诊断；
- `diagnostics.unchanged`：两次都存在的诊断（取较小出现次数）；
- `documents.added` / `removed` / `changed`：两次导入间新增、删除的文档路径，
  以及路径相同但内容发生变化的路径；
- 三组诊断均按（路径、行号、类型、消息）确定性排序，文档级诊断（无行号）排在最前。

```json
{
  "base_validation_id": "…",
  "target_validation_id": "…",
  "base_import_id": "…",
  "target_import_id": "…",
  "diagnostics": {
    "added": [
      {"type": "broken_anchor", "severity": "error", "path": "docs/a.md",
       "line": 3, "message": "anchor '#x' not found in 'index.md'",
       "details": {"target": "../index.md#x", "resolved": "index.md",
                   "fragment": "x", "available": ["home"]}}
    ],
    "removed": [],
    "unchanged": [],
    "counts": {"added": 1, "removed": 0, "unchanged": 0}
  },
  "documents": {"added": ["docs/b.md"], "removed": [], "changed": ["docs/a.md"]}
}
```

任一校验不存在返回 `404`（`validation not found`）；`pending`/`running`/
`failed` 状态返回 `409` 并在 `detail` 中说明当前状态；请求体缺少字段或为空
返回 `422`。两次校验可以指向同一导入，也允许与自身对比（结果为零差异）。

## SARIF 导出

`GET /api/validations/{validation_id}/sarif` 返回符合
[SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
的报告，媒体类型为 `application/sarif+json`，可直接导入 GitHub code scanning、
VS Code SARIF Viewer 等工具：

- 每个校验任务对应一个 `run`，四类诊断在 `tool.driver.rules` 中有稳定的规则定义：
  `broken_link`（error）、`broken_anchor`（error）、`anchor_collision`（warning
  默认；显式锚点重复定义时结果级别覆盖为 error）、`cycle`（warning）。
- 每个结果保留文档路径（`physicalLocation.artifactLocation.uri`，相对路径，
  空格等字符按 URI 百分号编码）、1-based 行号（`region.startLine`；循环为
  文档级诊断，无 region）、`ruleId`/`ruleIndex`、与接口一致的可定位 `message`，
  原始诊断信息放在 `properties.details` 中。
- 结果按（文档路径、行号、规则、消息）确定性排序，行号缺失的文档级诊断排在最前；
  重复导出逐字节一致。
- 仅 `done` 状态可导出：校验不存在返回 `404`，`pending`/`running`/`failed`
  返回 `409` 并在 `detail` 中说明当前状态。

## 解析规则与已知限制

- 锚点生成遵循 GitHub 规则：小写、去标点、空白转 `-`，重复标题自动追加 `-1`、`-2`；
  支持 `{#custom-id}` 显式锚点（kramdown/Pandoc 风格）。
- 仅识别 ATX 标题（`#`），不支持 Setext（`===`/`---`）标题。
- 外部链接（`http:`、`mailto:` 等）与协议相对链接（`//host`）不参与校验。
- 链接目标按 POSIX 相对路径解析；以 `/` 开头视为仓库根目录绝对路径；
  `%20` 等百分号编码在比较前解码。
- 仅被实际引用的 reference-style 定义（`[label]: target`）计入引用图。
- 行内链接目的地不支持未转义的嵌套括号（CommonMark 本身亦建议用 `<>` 包裹）。
- 缩进代码块按"4 空格/Tab 且前接空行"的简化规则识别。

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由，启动时自动初始化数据库
  parser.py    # Markdown 解析：标题/锚点/链接/代码块
  analyzer.py  # 引用图构建与诊断（断链/断锚/碰撞/环）
  comparison.py# 两次校验结果对比：诊断指纹差分 + 文档集合差分
  fixer.py     # 修复预览：最小化重写锚点与引用
  sarif.py     # SARIF 2.1.0 导出：诊断 -> runs/results 映射
  db.py        # SQLite 持久层（imports / documents / validations / diagnostics）
  schemas.py   # 请求模型与路径校验
tests/
  test_api.py  # 解析器、分析器与 API 端到端测试
```
