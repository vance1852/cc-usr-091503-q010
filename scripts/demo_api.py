"""端到端 HTTP 演示：题目场景完整流程。

运行：先 `python manage.py seed_demo && python manage.py runserver`，
再执行 `python scripts/demo_api.py`。
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title, status, body, key_fields=()):
    print(f"\n--- {title} [{status}]")
    if key_fields:
        print(json.dumps({k: body.get(k) for k in key_fields},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(body, ensure_ascii=False, indent=2, default=str)[:2000])


def main():
    case_id, contact_id = 1, 1
    staff = {s["role"]: s["id"] for s in call("GET", "/staff/")[1]}
    print("人员:", staff)

    # 1) 构建婴儿包：夜观/用品已确认；营养建议尚未确认，不得带入
    status, packet = call("POST", "/packets/build/",
                          {"case_id": case_id, "subject": "baby"})
    show("1. 从时间线挑选已确认事实生成婴儿包草稿", status, packet,
         ("version", "status"))
    for section in packet["sections"]:
        print(f"   [{section['kind_display']}] {section['state_display']} "
              f"负责={section['responsible_name']}\n     {section['content'][:80]}")

    packet_id = packet["id"]

    # 2) 未签署直接发布 -> 必须被阻断
    status, blocked = call("POST", f"/packets/{packet_id}/publish/")
    show("2. 未签署直接发布（应阻断）", status, blocked)

    # 3) 月嫂/专员各自签署自己的板块
    for section in packet["sections"]:
        if section["state"] == "draft":
            status, signed = call("POST", f"/sections/{section['id']}/sign/",
                                  {"staff_id": section["responsible"]})
            print(f"3. 签署 {section['kind_display']}: [{status}] {signed}")

    # 4) 发布 v1 并送达家庭，家庭查看（回执）
    status, v1 = call("POST", f"/packets/{packet_id}/publish/")
    show("4a. 婴儿包 v1 发布", status, v1, ("version", "status", "published_at"))
    status, delivery = call("POST", f"/packets/{packet_id}/deliver/",
                            {"contact_id": contact_id})
    show("4b. 送达周爸爸", status, delivery, ("delivered_at", "viewed_at"))
    call("POST", f"/deliveries/{delivery['id']}/view/")
    print("4c. 家庭已查看，形成回执")

    # 5) 家庭决定提前一天离馆
    _, case = call("GET", f"/cases/{case_id}/")
    from datetime import datetime, timedelta
    old_at = datetime.fromisoformat(case["planned_discharge_at"])
    new_at = (old_at - timedelta(days=1)).isoformat()
    status, case = call("POST", f"/cases/{case_id}/reschedule/",
                        {"planned_discharge_at": new_at})
    show("5. 提前一天离馆，已发布 v1 不静默改动", status, case,
         ("planned_discharge_at", "transfer_status"))

    # 6) v2 更正草稿生成（纳入最新已确认事实），重签后发布
    status, v2 = call("POST", "/packets/build/",
                      {"case_id": case_id, "subject": "baby"})
    show("6a. 生成 v2 更正草稿", status, v2, ("version", "status"))
    print("    版本变化:", [c["text"] for c in v2["changes"]])
    for section in v2["sections"]:
        if section["state"] == "draft":
            call("POST", f"/sections/{section['id']}/sign/",
                 {"staff_id": section["responsible"]})
    status, v2 = call("POST", f"/packets/{v2['id']}/publish/")
    show("6b. v2 发布（v1 自动被替代）", status, v2, ("version", "status"))

    # 7) 已查看旧版的授权联系人收到更正通知
    _, notes = call("GET", f"/notifications/?contact={contact_id}")
    show("7. 通知已查看过旧版的联系人", 200, notes)

    # 8) 产妇包：营养师饮食建议仍待确认 -> 板块显式待处理，不空白
    status, mother = call("POST", "/packets/build/",
                          {"case_id": case_id, "subject": "mother"})
    pending = [s for s in mother["sections"] if s["state"] == "pending_data"]
    print(f"8. 产妇包待处理板块 {len(pending)} 个（不得空白）:")
    for section in pending:
        print(f"   - {section['kind_display']}: {section['content']}")
    for section in mother["sections"]:
        if section["state"] == "draft":
            call("POST", f"/sections/{section['id']}/sign/",
                 {"staff_id": section["responsible"]})
    status, mother = call("POST", f"/packets/{mother['id']}/publish/")
    print(f"   产妇包发布: [{status}] (待处理异常显式随行)")

    # 9) 三种交接路径之一：提前离馆 -> 家属确认后责任转移
    status, _ = call("POST", f"/cases/{case_id}/transfer-path/",
                     {"path": "early", "target_name": "自行返家"})
    status, bad = call("POST", f"/cases/{case_id}/transfer-ack/", {})
    show("9a. 无家属确认不能转移责任", status, bad)
    status, acked = call("POST", f"/cases/{case_id}/transfer-ack/",
                         {"contact_id": contact_id})
    show("9b. 家属确认，责任转移完成", status, acked,
         ("path_display", "transfer_status_display", "responsibility_transferred"))

    # 10) 家庭交接页 + 服务专员追踪
    _, page = call("GET", f"/cases/{case_id}/handoff-page/")
    print("\n10. 家庭交接页结构:", list(page.keys()))
    baby_page = page["current_effective_advice"]["baby"]
    print("    婴儿当前有效版本: v" + str(baby_page["current_version"]))
    _, tracking = call("GET", f"/cases/{case_id}/tracking/")
    print("    专员追踪版本数:", len(tracking["packets"]),
          " 未闭环异常:", len(tracking["open_anomalies"]))


if __name__ == "__main__":
    main()
