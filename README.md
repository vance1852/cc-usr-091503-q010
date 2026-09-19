# 母婴离馆照护衔接

本项目帮助出馆服务人员整理母亲与婴儿的返家摘要、待办事项和后续联系记录。应用采用 Python（Django REST Framework + SQLite），支持多专业确认、责任转移和发布后的版本更正。

## 核心规则

- **只带确认过的事实回家**：摘要仅从时间线中"已确认"的事实生成；待确认的事实不进入建议，但在对应章节中明确列出，不用空白掩盖。
- **分对象、带版本**：产妇与婴儿各一份摘要，每次发布产生一个版本；发布后更正生成新版本，旧版本标记为"已废止"。
- **专业签署**：夜间照护（月嫂）、饮食建议（营养师）、用品说明（护士）、待办与联系（服务专员）各章节须由对应角色签署后才能发布；签署并发安全（同一章节只允许一个签署人）。
- **异常不掩盖**：未闭环的阻断级异常直接阻止发布（409 并返回异常清单）；提示级异常在摘要"待处理异常"章节中明确标为"待处理"。
- **交接路径**：提前离馆 / 转往其他机构 / 月嫂上门服务三种路径各有必填校验；接收方确认后才完成责任转移。
- **离馆时间变更**：随访计划按相对偏移重排、待办顺延、待确认交接时间更新，并通知全部授权联系人。
- **更正通知**：新版本发布后，查看过旧版本的授权联系人会收到更正通知；每个版本的送达与回执全程可追踪。

## 运行

```bash
pip install django djangorestframework
python3 manage.py migrate
python3 manage.py runserver
```

## 测试

```bash
python3 manage.py test handover
```

覆盖：并发签署（不同章节并行成功 / 同一章节仅一人成功）、离馆时间变更（重排与通知）、旧版通知（仅查看过旧版的联系人收到）、阻断异常、角色权限、三种交接路径、家庭交接页与专员追踪。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/cases/` | 创建家庭案例 |
| POST | `/api/cases/{id}/timeline_events/` | 录入时间线事实（待确认） |
| POST | `/api/timeline-events/{id}/confirm/` | 对应角色确认事实 |
| POST | `/api/cases/{id}/compose/` | 生成产妇/婴儿两份摘要草稿 |
| POST | `/api/summary-versions/{vid}/sections/{sid}/sign/` | 专业人员签署章节 |
| POST | `/api/summary-versions/{vid}/publish/` | 发布版本（校验签署与阻断异常） |
| POST | `/api/summaries/{id}/correct/` | 更正：生成新版本草稿 |
| POST | `/api/cases/{id}/handovers/` | 发起交接（三种路径） |
| POST | `/api/handovers/{id}/receiver-confirm/` | 接收方确认，完成责任转移 |
| POST | `/api/cases/{id}/change-discharge/` | 变更离馆时间 |
| GET | `/api/cases/{id}/handover-page/?contact_id=` | 家庭交接页（当前建议/待办/联系渠道/版本变化） |
| GET | `/api/cases/{id}/tracking/` | 专员追踪（签署人/时间、送达与回执、交接进展） |
| GET | `/api/notifications/?case=` | 通知记录 |

## 目录结构

- `handover/models.py` — 案例、时间线、摘要版本/章节、异常、交接、联系人、送达回执、通知
- `handover/services.py` — 业务逻辑（确认、生成、签署、发布、更正、交接、离馆变更、交接页、追踪）
- `handover/views.py` / `urls.py` — REST API
- `handover/tests.py` — 21 个测试，含并发签署（`TransactionTestCase` + 文件型 SQLite + `transaction_mode=IMMEDIATE`）
