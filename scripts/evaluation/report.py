"""Recompute summaries from raw records; never accept hand-entered result metrics."""

import argparse
import hashlib
import json
import math
from collections import Counter

from evaluation.run import DELIVERY, write


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def records(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(n=0, p50=None, p95=None, maximum=None)
    return dict(
        n=len(values),
        p50=values[math.ceil(len(values) * 0.5) - 1],
        p95=values[math.ceil(len(values) * 0.95) - 1],
        maximum=values[-1],
    )


def needs_human(record):
    return any(
        e["kind"] in ("handoff", "error") and e["payload"].get("requires_human") is True
        for t in record.get("turns", [])
        for e in t.get("events", [])
    )


def summarize(raw):
    turns = [t for r in raw for t in r.get("turns", []) if "run" in t]
    calls = [call for turn in turns for call in turn["run"].get("usage", {}).get("calls", [])]
    return dict(
        cases=len(raw),
        passed=sum(r["passed"] for r in raw),
        failed=sum(not r["passed"] for r in raw),
        splits={
            split: dict(
                cases=sum(r["split"] == split for r in raw),
                passed=sum(r["split"] == split and r["passed"] for r in raw),
            )
            for split in ("dev", "frozen")
        },
        categories={
            category: dict(
                cases=sum(r["category"] == category for r in raw),
                passed=sum(r["category"] == category and r["passed"] for r in raw),
            )
            for category in sorted({r["category"] for r in raw})
        },
        admission_ms=distribution([t["admission_ms"] for t in turns]),
        first_response_ms=distribution([t.get("first_response_ms") for t in turns]),
        full_task_ms=distribution([t.get("full_task_ms") for t in turns]),
        terminal_task_states=dict(Counter(t["run"]["status"] for t in turns)),
        usage=dict(
            calls=len(calls),
            input_tokens=sum(c.get("input_tokens", 0) for c in calls),
            output_tokens=sum(c.get("output_tokens", 0) for c in calls),
            total_tokens=sum(c.get("total_tokens", 0) for c in calls),
            returned_models=sorted(
                {
                    model
                    for t in turns
                    for model in t["run"].get("usage", {}).get("returned_models", [])
                    if model
                }
            ),
            monetary_cost=None,
            monetary_cost_status="未取得账单或可核验对应单价；仅记录实际 tokens",
        ),
        extra_handoff_cases=sum(needs_human(r) for r in raw),
        unclassified_failure_cases=sum(
            not needs_human(r)
            and any(e["kind"] == "error" for t in r.get("turns", []) for e in t.get("events", []))
            for r in raw
        ),
        failures=[
            dict(
                case_id=r["case_id"],
                category=r["category"],
                split=r["split"],
                assertions=r.get("failures", []),
                harness_error=r.get("harness_error"),
            )
            for r in raw
            if not r["passed"]
        ],
    )


def generate(run_id):
    output = DELIVERY / "runs" / run_id
    manifest = read(output / "manifest.json")
    raw = {host["name"]: records(output / (host["name"] + ".jsonl")) for host in manifest["hosts"]}
    original_variants = {name: summarize(data) for name, data in raw.items()}
    from evaluation.oracle import audit

    raw, oracle_audit = audit(DELIVERY, output, raw)
    variants = {name: summarize(data) for name, data in raw.items()}
    casesummary = read(output / "case-summary.json", {})
    load_chat = records(output / "load-chat.jsonl")
    load_execution = records(output / "load-execution.jsonl")
    replay = read(output / "replay-summary.json", {})
    faults = read(output / "faults.json", [])
    replay_records = records(output / "replay.jsonl")
    source_integrity = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.glob("*.json*")
        if path.name != "summary.json"
    }
    result = dict(
        run_id=run_id,
        manifest=manifest,
        variants=variants,
        original_v1_variants=original_variants,
        grading_audit=oracle_audit,
        load=read(output / "load-summary.json", {}),
        replay=replay,
        faults=faults,
        load_metrics=dict(
            chat={
                name: distribution([t.get(name) for t in load_chat])
                for name in ("admission_ms", "first_response_ms", "full_task_ms")
            },
            execution={
                name: distribution([t.get(name) for t in load_execution])
                for name in ("admission_ms", "full_task_ms")
            },
        ),
        gates=dict(
            offline_case_counts=all(
                variants.get(name, {}).get("cases") == 300
                for name in ("baseline_offline", "final_offline")
            ),
            frozen_case_counts=all(
                variants.get(name, {}).get("splits", {}).get("frozen", {}).get("cases") == 150
                for name in ("baseline_offline", "final_offline")
            ),
            real_samples=all(
                variants.get(name, {}).get("cases") == 30 and variants[name]["usage"]["calls"] > 0
                for name in ("baseline_real", "final_real")
            ),
            business_unchanged=bool(casesummary)
            and all(s["business_unchanged"] for s in casesummary.values()),
            replay_complete=len(replay_records) == 2000
            and len({r["sequence"] for r in replay_records}) == 2000
            and replay.get("all_offsets_acknowledged") is True,
            zero_duplicate_side_effects=replay.get("duplicate_side_effects") == 0
            and replay.get("before") == replay.get("after"),
            load_50=len(load_chat) == len(load_execution) == 50
            and all(r.get("run", {}).get("status") == "completed" for r in load_chat)
            and all(r["result"]["job"]["status"] == "completed" for r in load_execution),
            fault_scenarios=len(faults) == 8 and all(f["passed"] for f in faults),
        ),
        raw_files_sha256=source_integrity,
    )
    write(output / "summary.json", result)
    write(DELIVERY / "summary.json", result)
    md = [
        "# 3.2 实测报告",
        "",
        f"运行：`{run_id}`。结果从逐条 JSONL 和数据库核对文件重新计算。",
        "",
        "| 模式 | 案例通过/总数 | 冻结集通过/总数 | 受理 p95(ms) | 首条有效响应 p95(ms) | 完整任务 p95(ms) | 实际 tokens |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in variants.items():
        frozen = value["splits"]["frozen"]
        md.append(
            f"| {name} | {value['passed']}/{value['cases']} | {frozen['passed']}/{frozen['cases']} | {value['admission_ms']['p95']} | {value['first_response_ms']['p95']} | {value['full_task_ms']['p95']} | {value['usage']['total_tokens']} |"
        )
    md += [
        "",
        "计分为 v1.1 政策审计修正：冻结输入和原始 v1 结果未改变。发现 10 条换货/维修首轮非收货请求的基准答案错误，依据既有 v3 政策修正首轮预期；没有新增模型调用。这是观察结果后的计分修正，原始评分同时保留，不能冒充独立盲测。",
        "",
        "原始 v1 通过数："
        + "；".join(
            f"{name} {value['passed']}/{value['cases']}"
            for name, value in original_variants.items()
        ),
        "",
        "详见 [评分审计](oracle-corrections.json) 和 [修正定义](cases-policy-corrected.json)。",
    ]
    md += [
        "",
        "baseline 是基础 Agent 图在共同基础设施上的对照；final 是 Supervisor。offline 是确定性提取器，不是大模型效果。real 仅为每版 30 条预选样本。",
        "",
        "时长包含轮询开销，首响应不计排队状态和路由消息；完整任务统计所有取得终态的任务，含正确拒绝和功能误判。协议见 [评测协议](评测协议.md)。",
        "",
        "## 系统验证",
        "",
        "| 检查 | 通过 |",
        "|---|---|",
    ]
    md += [
        f"| {name} | {'是' if passed else '未完成'} |" for name, passed in result["gates"].items()
    ]
    md += [
        "",
        f"重放 {replay.get('messages', 0)} 条消息，{replay.get('distinct_tasks', 0)} 个不同已完成任务；等待消费组提交 offset 后比对工单、退款、发货、维修和相关业务摘要。",
        "",
        "50 并发为客户端在途任务，实际消费者为 4。离线并发窗口与真实模型采样分开统计。",
        "",
    ]
    for kind, metrics in result["load_metrics"].items():
        md += [
            f"- {kind}："
            + "；".join(
                f"{name} p50={values['p50']}、p95={values['p95']}、max={values['maximum']} ms"
                for name, values in metrics.items()
            )
        ]
    md += [
        "",
        "故障宿主真实进程被终止；Redis/producer 异常在客户端边界注入，producer 重试时间被加速，不能据此声称物理服务故障恢复耗时。",
        "",
        "## 失败与费用",
        "",
        "所有失败 ID、逐轮断言和实际输出见交互报告及以下原始记录。功能评测失败不删除，不把安全拒绝混作错误或把功能误判藏进安全通过。",
        "",
    ]
    for name, value in variants.items():
        failed_categories = dict(Counter(f["category"] for f in value["failures"]))
        md.append(
            f"- {name}：失败分类 `{json.dumps(failed_categories, ensure_ascii=False)}`；显式人工升级 {value['extra_handoff_cases']}/{value['cases']}，未标注人工升级的错误案例 {value['unclassified_failure_cases']}。规定审批不计入该数；这不是实际客服处理率。模型调用 {value['usage']['calls']}，输入 {value['usage']['input_tokens']}、输出 {value['usage']['output_tokens']} tokens。"
        )
    md += [
        "",
        "货币费用未取得实际账单或对应可核验单价，因此标注未核验；不将 tokens 换算为猜测金额。",
        "",
        "## 原始证据",
        "",
    ]
    md += [
        f"- [{path.name}](runs/{run_id}/{path.name})"
        for path in sorted(output.glob("*.json*"))
        if path.name != "summary.json"
    ]
    (DELIVERY / "实测报告.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    compact = []
    for name, data in raw.items():
        for record in data:
            compact.append(
                dict(
                    host=name,
                    case_id=record["case_id"],
                    split=record["split"],
                    category=record["category"],
                    passed=record["passed"],
                    failures=record.get("failures", []),
                    harness_error=record.get("harness_error"),
                    turns=[
                        dict(
                            message=t.get("message"),
                            http_status=t.get("http_status"),
                            run_id=t.get("run", {}).get("id"),
                            full_task_ms=t.get("full_task_ms"),
                            previews=[
                                e["payload"] for e in t.get("events", []) if e["kind"] == "preview"
                            ],
                            handoff=[
                                e["payload"]
                                for e in t.get("events", [])
                                if e["kind"] in ("handoff", "error")
                            ],
                        )
                        for t in record.get("turns", [])
                    ],
                )
            )
    payload = json.dumps(dict(result=result, cases=compact), ensure_ascii=False).replace(
        "<", "\\u003c"
    )
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>3.2 评测与故障演练</title>
<style>body{font:16px system-ui;background:#f3f7f5;color:#203d35;max-width:1200px;margin:32px auto;padding:16px}h1{font-size:30px}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #dae5df}select,label{margin:8px}details{background:white;padding:12px;margin:10px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f7f5;padding:12px}a{color:#146448}.bad{color:#a13e32}.ok{color:#156742}button{padding:8px}</style>
<h1>3.2 评测与故障演练</h1><p id="run"></p><p>300 条模板案例（150 开发 / 150 冻结）；真实模型每版仅 30 条预选样本。对照历史 Agent 图，共用当前基础设施。</p>
<p>计分为 v1.1 政策审计修正；输入和原始 v1 结果保留，修正了 10 条非收货换货/维修首轮的错误预期，没有新增模型调用。修正是在观察结果后进行，不能视为独立盲测。</p><p id="original"></p><div id="metrics"></div><h2>系统证据</h2><div id="gates"></div><p>50 是客户端在途并发，4 是消费者数量。Redis / producer 连接异常由评测脚本注入；进程重启为实际终止并重建。费用只核对实际 tokens，货币金额尚未核验。</p>
<h2>逐条案例</h2><select id="variant"><option value="">所有版本</option></select><select id="split"><option value="">开发＋冻结</option><option>dev</option><option>frozen</option></select><select id="category"><option value="">所有场景</option></select><label><input type="checkbox" id="failed">仅显示失败</label><p id="count"></p><div id="cases"></div><h2>原始文件</h2><div id="links"></div>
<script id="data" type="application/json">PAYLOAD</script><script>
const d=JSON.parse(document.getElementById('data').textContent),r=d.result;const names={baseline_offline:'单 Agent · 离线',final_offline:'多 Agent · 离线',baseline_real:'单 Agent · 百炼',final_real:'多 Agent · 百炼'};const gates={offline_case_counts:'离线案例数量：两版各 300',frozen_case_counts:'冻结集数量：两版各 150',real_samples:'百炼抽样数量：两版各 30',business_unchanged:'咨询未改动业务表',replay_complete:'2,000 条重放已完成消费确认',zero_duplicate_side_effects:'重复业务副作用为 0',load_50:'50 个在途咨询及执行任务均完成',fault_scenarios:'8 项故障演练完成'};const variant=document.getElementById('variant'),split=document.getElementById('split'),category=document.getElementById('category'),failed=document.getElementById('failed');const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));document.getElementById('run').textContent='运行 '+r.run_id;
document.getElementById('metrics').innerHTML='<table><tr><th>模式</th><th>通过 / 总数</th><th>冻结集</th><th>受理 p95(ms)</th><th>首响应 p95(ms)</th><th>完整任务 p95(ms)</th></tr>'+Object.entries(r.variants).map(([k,v])=>`<tr><td>${esc(names[k]||k)}</td><td>${v.passed}/${v.cases}</td><td>${v.splits.frozen.passed}/${v.splits.frozen.cases}</td><td>${v.admission_ms.p95}</td><td>${v.first_response_ms.p95}</td><td>${v.full_task_ms.p95}</td></tr>`).join('')+'</table>';
document.getElementById('original').textContent='原始 v1 通过数：'+Object.entries(r.original_v1_variants).map(([k,v])=>(names[k]||k)+' '+v.passed+'/'+v.cases).join('；');document.getElementById('gates').innerHTML=Object.entries(r.gates).map(([k,v])=>`<p class="${v?'ok':'bad'}">${esc(gates[k]||k)}：${v?'通过':'未完成'}</p>`).join('');
for(const [id,values] of [['variant',Object.keys(r.variants)],['category',[...new Set(d.cases.map(c=>c.category))].sort()]])for(const value of values){const o=document.createElement('option');o.value=value;o.textContent=id==='variant'?(names[value]||value):value;document.getElementById(id).append(o)}
function render(){const list=d.cases.filter(c=>(!variant.value||c.host===variant.value)&&(!split.value||c.split===split.value)&&(!category.value||c.category===category.value)&&(!failed.checked||!c.passed));document.getElementById('count').textContent='匹配 '+list.length+' 条；展开查看逐轮实际结果和失败断言';document.getElementById('cases').innerHTML=list.map(c=>`<details><summary class="${c.passed?'ok':'bad'}">${esc(c.case_id)} · ${esc(c.host)} · ${esc(c.split)} · ${esc(c.category)} · ${c.passed?'通过':'失败'}</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details>`).join('')}for(const id of ['variant','split','category','failed'])document.getElementById(id).addEventListener('change',render);render();
document.getElementById('links').innerHTML=Object.keys(r.raw_files_sha256).map(f=>`<p><a href="runs/${encodeURIComponent(r.run_id)}/${encodeURIComponent(f)}">${esc(f)}</a></p>`).join('');</script></html>""".replace(
        "PAYLOAD", payload
    )
    (DELIVERY / "评测报告.html").write_text(page, encoding="utf-8")
    print(
        json.dumps(
            dict(
                run_id=run_id,
                gates=result["gates"],
                variants={
                    k: dict(passed=v["passed"], cases=v["cases"]) for k, v in variants.items()
                },
            ),
            ensure_ascii=False,
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args = parser.parse_args()
    generate(args.run_id or read(DELIVERY / "latest-run.json")["run_id"])
