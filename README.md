# 母婴离馆衔接平台

面向月子中心出馆服务专员的交接平台：从中心照护时间线中**只挑选已确认事实**，
为产妇与婴儿分别生成带版本的返家摘要、待办事项、用品说明与后续联系记录，
并支持专业人员分科签署、发布闸门、三种交接路径与版本更正通知。

技术栈：Python · Django REST Framework · SQLite（WAL）。

## 业务规则

1. **已确认事实才入包**：月嫂夜间观察、营养师饮食建议、婴儿用品注意事项等
   `CareEvent` 处于"待确认"阶段时绝不进入交接包；已发布事实被更正后回到待确认，
   必须重新确认并经新版本发布。
2. **各签各的**：每个板块（摘要/待办/用品/联系）绑定唯一负责专业人员，
   月嫂不能签营养师的部分；板块事实集合变化时旧签署自动失效。
3. **发布闸门明确阻断**：
   - 未签署板块、内容空白、未闭环 `BLOCK` 异常 → 400 阻断并逐条列出原因；
   - 某专业线暂无已确认事实 → 板块显式标为 `pending_data`（待处理）并登记
     `PENDING` 异常，**绝不用空白掩盖**，可随包发布并在交接页突出展示。
4. **三种交接路径**：
   - 提前离馆返家 → 家属授权联系人确认；
   - 转往其他机构 → 接收机构人员（receiver 角色）确认；
   - 月嫂上门 → 月嫂（nanny 角色）确认。
   接收方确认前责任不转移，且产妇/婴儿当前建议均须已发布。
5. **版本与通知**：离馆时间变更（如提前一天）不静默改动已发布版本，而是生成
   更正草稿与版本变化说明；新版本发布后自动通知**查看过旧版**的授权联系人，
   未查看者不打扰。
6. **送达追踪**：服务专员可追踪每部分由谁确认、何时送达、是否回执（已查看）。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install django djangorestframework
python manage.py migrate
python manage.py seed_demo          # 构造题目场景演示数据
python manage.py runserver
```

端到端 HTTP 演示（另开终端，服务启动后执行）：

```bash
python scripts/demo_api.py
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/events/{id}/confirm/` | 对应角色专业人员确认时间线事实 |
| POST | `/api/events/{id}/correct/` | 更正事实（已发布过则失效回待确认） |
| POST | `/api/packets/build/` | 从已确认事实生成/刷新带版本草稿 |
| POST | `/api/sections/{id}/sign/` | 负责专业人员签署自己的板块 |
| POST | `/api/packets/{id}/publish/` | 发布闸门：不满足条件明确阻断 |
| POST | `/api/packets/{id}/deliver/` | 向授权联系人送达已发布版本 |
| POST | `/api/deliveries/{id}/view/` | 家庭查看，形成回执 |
| POST | `/api/cases/{id}/reschedule/` | 离馆时间变更，生成更正草稿 |
| POST | `/api/cases/{id}/transfer-path/` | 设置三种交接路径与接收目标 |
| POST | `/api/cases/{id}/transfer-ack/` | 接收方确认，责任转移 |
| GET | `/api/cases/{id}/handoff-page/` | 家庭交接页（有效建议/待办/渠道/版本变化） |
| GET | `/api/cases/{id}/tracking/` | 专员追踪（确认人/送达/回执） |
| GET | `/api/notifications/?contact=<id>` | 旧版查看人的新版本通知 |

## 模型概览

- `CareEvent`：中心照护时间线（类别→负责角色、状态→仅 confirmed 入包）
- `DischargeCase`：离馆案件 + 交接路径与责任转移状态
- `HandoffPacket` / `PacketSection` / `SectionConfirmation`：产妇、婴儿各自的
  带版本交接包、四类板块与专业人员签署（OneToOne 唯一约束防并发重复签署）
- `Anomaly`：BLOCK（阻断发布）/ PENDING（显式待处理）
- `Delivery` / `Notification` / `VersionChange`：送达回执、旧版更正通知、版本变化
- `AuthorizedContact`：授权家属联系人

## 测试

```bash
python manage.py test handoff
```

16 个测试覆盖：

- **并发签署**：8 线程同时签署同一板块，仅 1 人成功，其余被唯一约束与显式校验拒绝；
- **离馆时间变更**：提前离馆后已发布版本不被改动，v2 草稿附版本变化说明，
  发布后 v1 标记为被替代；
- **旧版通知**：更正发布新版本后仅通知查看过旧版的联系人，且通知版本号正确；
- 发布闸门阻断/待处理标记、越权签署与确认、三种路径的不同确认人、
  家庭交接页与专员追踪，以及完整 HTTP 故事线。

> 并发测试使用文件型 SQLite + WAL（`settings.DATABASES['default']['TEST']`），
> 因共享缓存内存库不支持真正的多连接并发写。
