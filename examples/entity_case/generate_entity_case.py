#!/usr/bin/env python3
"""
生成实体调查案例：调查客户「张伟华」的完整画像
"""
import pandas as pd
from pathlib import Path

OUT = Path(__file__).parent

def main():
    print("生成实体调查案例数据...")

    # 合同记录
    contracts = pd.DataFrame([
        {"合同编号":"CTR-2023-001","甲方":"张伟华","乙方":"高科技有限公司","金额":288000,"签署日期":"2023-03-15","状态":"履行中"},
        {"合同编号":"CTR-2023-047","甲方":"张伟华","乙方":"云服务平台","金额":56000,"签署日期":"2023-06-20","状态":"已完成"},
        {"合同编号":"CTR-2022-198","甲方":"张伟华企业","乙方":"供应商A","金额":120000,"签署日期":"2022-11-08","状态":"已完成"},
    ])
    contracts.to_excel(OUT/"contracts.xlsx", index=False)

    # 人员档案
    profiles = pd.DataFrame([
        {"姓名":"张伟华","证件号":"110105198801010012","联系电话":"13812345678","所在城市":"北京","单位":"张伟华企业","职务":"法人代表","备注":"VIP客户"},
        {"姓名":"张伟","证件号":"110105199001020034","联系电话":"13987654321","所在城市":"上海","单位":"华丰集团","职务":"总监","备注":""},
        {"姓名":"李伟华","证件号":"310101198502030056","联系电话":"13711112222","所在城市":"广州","单位":"南方科技","职务":"经理","备注":""},
    ])
    profiles.to_excel(OUT/"profiles.xlsx", index=False)

    # 交易记录
    transactions = pd.DataFrame([
        {"交易ID":"TXN-8801","客户姓名":"张伟华","金额":28800,"交易日期":"2023-07-20","类型":"采购","对手方":"设备供应商","状态":"完成"},
        {"交易ID":"TXN-8802","客户姓名":"张伟华","金额":15000,"交易日期":"2023-08-05","类型":"服务费","对手方":"咨询公司","状态":"完成"},
        {"交易ID":"TXN-9901","客户姓名":"张伟华","金额":500000,"交易日期":"2023-09-01","类型":"大额转账","对手方":"境外账户","状态":"待核查","备注":"超额预警"},
    ])
    transactions.to_excel(OUT/"transactions.xlsx", index=False)

    # 背调报告（文本）
    with open(OUT/"background.md","w",encoding="utf-8") as f:
        f.write("""# 张伟华背景调查摘要

## 基本信息
- 姓名：张伟华
- 所在城市：北京
- 企业：张伟华企业（法人代表）
- 信用等级：A级（截至2023年）

## 已知关联实体
- 高科技有限公司（签署合同CTR-2023-001，金额28.8万）
- 云服务平台（签署合同CTR-2023-047，金额5.6万）
- 供应商A（历史合作，已完成）

## 异常记录
规则：大额转账 > 10万元 需要双重审核。
2023-09-01 发现一笔50万元大额转账至境外账户，当前状态「待核查」，触发预警规则。

## 核查结论（暂疑）
张伟华整体信用记录良好，但2023年9月存在一笔大额可疑转账，
建议核查该笔交易的合规性和资金来源。
""")

    print(f"✓ contracts.xlsx — 3条合同")
    print(f"✓ profiles.xlsx  — 3条人员档案（含张伟华）")
    print(f"✓ transactions.xlsx — 3条交易（含异常）")
    print(f"✓ background.md  — 背调文字报告")
    print(f"\n运行调查:")
    print(f"  python ../../scripts/detective.py investigate \\")
    print(f"    --mode entity --target 张伟华 \\")
    print(f"    --sources contracts.xlsx profiles.xlsx transactions.xlsx background.md \\")
    print(f"    --out report.json --html wall.html --verbose")

if __name__ == "__main__":
    main()
