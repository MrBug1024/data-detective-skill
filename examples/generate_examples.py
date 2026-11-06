#!/usr/bin/env python3
"""
generate_examples.py — 生成演示用的 A/B/C/D 业务表和 R1 结果表

设计目标：R1.xlsx 第1行（row=2）的结果由以下产生：
  - A.xlsx  第500行
  - B.xlsx  第750行
  - C.xlsx  第30行
  - D.xlsx  第76行
  - rules.md 中的相关计算规则
"""
import os
import random
import pandas as pd
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent

random.seed(42)


def generate_A():
    """A表：客户信息表，5000行"""
    print("生成 A.xlsx (客户信息表, 5000行)...")
    rows = []
    for i in range(1, 5001):
        rows.append({
            "客户ID": f"C{i:06d}",
            "客户姓名": f"客户_{i}",
            "联系电话": f"138{i:08d}"[:11],
            "所在城市": random.choice(["北京", "上海", "广州", "深圳", "成都", "武汉"]),
            "信用等级": random.choice(["A", "B", "C", "D"]),
            "注册日期": f"202{random.randint(0,3)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "备注": f"客户备注信息_{i}" if i % 10 == 0 else None,
        })
    # 先设置目标行
    rows[498] = {
        "客户ID": "C000500",
        "客户姓名": "张伟华",
        "联系电话": "13812345678",
        "所在城市": "北京",
        "信用等级": "A",
        "注册日期": "2021-03-15",
        "备注": "VIP客户",
    }
    # 再确保C000500只出现在第500行（index=498），清除其他行的重复
    for i, r in enumerate(rows):
        if i != 498 and r.get("客户ID") == "C000500":
            rows[i]["客户ID"] = f"C{90000+i:06d}"  # 改成不会冲突的ID
    df = pd.DataFrame(rows)
    path = OUTPUT_DIR / "A.xlsx"
    df.to_excel(path, index=False)
    print(f"  ✓ {path} ({len(rows)}行)")
    return rows[498]


def generate_B(customer_id: str):
    """B表：订单表，10000行"""
    print("生成 B.xlsx (订单表, 10000行)...")
    rows = []
    for i in range(1, 10001):
        cid = f"C{random.randint(1,5000):06d}"
        rows.append({
            "订单ID": f"ORD{i:08d}",
            "客户ID": cid,
            "产品代码": f"P{random.randint(1,200):04d}",
            "订单金额": round(random.uniform(100, 50000), 2),
            "下单日期": f"202{random.randint(2,3)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "订单状态": random.choice(["完成", "处理中", "已取消", "退款"]),
            "销售员": f"销售_{random.randint(1,50)}",
        })
    # 先设置目标行
    rows[748] = {
        "订单ID": "ORD00000750",
        "客户ID": "C000500",  # 关联A表第500行
        "产品代码": "P0042",
        "订单金额": 28800.00,
        "下单日期": "2023-07-20",
        "订单状态": "完成",
        "销售员": "销售_15",
    }
    # 清除其他行的C000500重复（确保唯一）
    for i, r in enumerate(rows):
        if i != 748 and r.get("客户ID") == "C000500":
            rows[i]["客户ID"] = f"C{80000+i:06d}"
    # 清除其他行的ORD00000750重复
    for i, r in enumerate(rows):
        if i != 748 and r.get("订单ID") == "ORD00000750":
            rows[i]["订单ID"] = f"ORD{90000000+i:08d}"
    df = pd.DataFrame(rows)
    path = OUTPUT_DIR / "B.xlsx"
    df.to_excel(path, index=False)
    print(f"  ✓ {path} ({len(rows)}行)")
    return rows[748]


def generate_C():
    """C表：产品表，500行"""
    print("生成 C.xlsx (产品信息表, 500行)...")
    rows = []
    for i in range(1, 501):
        rows.append({
            "产品代码": f"P{i:04d}",
            "产品名称": f"产品_{i}",
            "产品类别": random.choice(["电子", "服装", "食品", "家具", "化妆品"]),
            "单价": round(random.uniform(50, 5000), 2),
            "库存数量": random.randint(0, 1000),
            "供应商": f"供应商_{random.randint(1,30)}",
        })
    # 确保P0042只出现在第30行（清除其他行中的P0042）
    for i, r in enumerate(rows):
        if i != 28 and r.get("产品代码") == "P0042":
            rows[i]["产品代码"] = f"P{i+1:04d}_dup"  # 重命名避免冲突
    # 第30行是P0042
    rows[28] = {
        "产品代码": "P0042",
        "产品名称": "高端商务笔记本",
        "产品类别": "电子",
        "单价": 6800.00,
        "库存数量": 42,
        "供应商": "供应商_08",
    }
    df = pd.DataFrame(rows)
    path = OUTPUT_DIR / "C.xlsx"
    df.to_excel(path, index=False)
    print(f"  ✓ {path} ({len(rows)}行)")
    return rows[28]


def generate_D():
    """D表：回款记录表，2000行"""
    print("生成 D.xlsx (回款记录表, 2000行)...")
    rows = []
    for i in range(1, 2001):
        rows.append({
            "回款ID": f"PAY{i:06d}",
            "订单ID": f"ORD{random.randint(1,10000):08d}",
            "回款金额": round(random.uniform(100, 50000), 2),
            "回款日期": f"202{random.randint(2,3)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
            "回款方式": random.choice(["银行转账", "现金", "支票", "在线支付"]),
            "审核状态": random.choice(["已审核", "待审核", "拒绝"]),
        })
    # 第76行关联到ORD00000750
    rows[74] = {
        "回款ID": "PAY000076",
        "订单ID": "ORD00000750",  # 关联B表第750行
        "回款金额": 28800.00,
        "回款日期": "2023-08-05",
        "回款方式": "银行转账",
        "审核状态": "已审核",
    }
    df = pd.DataFrame(rows)
    path = OUTPUT_DIR / "D.xlsx"
    df.to_excel(path, index=False)
    print(f"  ✓ {path} ({len(rows)}行)")
    return rows[74]


def generate_rules():
    """rules.md：业务规则文档"""
    print("生成 rules.md (业务规则文档)...")
    content = """# 业务计算规则与定义

## 规则1：订单回款率计算
公式：回款率 = 回款金额 / 订单金额 × 100%
当回款率 ≥ 100% 时，订单状态标记为"完成回款"。
当回款率 < 50% 时，触发预警，通知销售员跟进。

## 规则2：客户信用等级评定
- A级：近12个月无逾期，回款率 ≥ 95%
- B级：近12个月逾期次数 ≤ 2，回款率 ≥ 80%
- C级：近12个月逾期次数 ≤ 5，回款率 ≥ 60%
- D级：其他情况

## 规则3：VIP客户认定
定义：累计订单金额 ≥ 100,000元 且 信用等级为A级的客户认定为VIP客户。
VIP客户享有专属折扣和优先服务。
客户ID = C000500 因满足VIP条件被标记为VIP。

## 规则4：汇总结果生成规则
结果表R1的生成逻辑：
- 结果字段"客户ID"来源于A表客户ID字段
- 结果字段"订单金额合计"= SUM(B表中该客户的所有订单金额)
- 结果字段"产品名称"来源于C表产品名称（通过B表订单中的产品代码关联）
- 结果字段"回款总额"= SUM(D表中该客户相关订单的回款金额)
- 结果字段"回款率"= 回款总额 / 订单金额合计 × 100%

## 规则5：数据质量校验
- 客户ID格式：C + 6位数字
- 订单ID格式：ORD + 8位数字
- 所有金额字段保留2位小数
- 日期格式：YYYY-MM-DD
"""
    path = OUTPUT_DIR / "rules.md"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"  ✓ {path}")


def generate_R1(a_row, b_row, c_row, d_row):
    """R1.xlsx：结果表，第1行由A500+B750+C30+D76产生"""
    print("生成 R1.xlsx (结果表)...")
    rows = []

    # 第1行：目标行，由A500+B750+C30+D76产生
    rows.append({
        "客户ID": "C000500",                       # ← A表第500行 客户ID
        "客户姓名": "张伟华",                       # ← A表第500行 客户姓名
        "产品代码": "P0042",                        # ← B表第750行 产品代码，C表第30行
        "产品名称": "高端商务笔记本",               # ← C表第30行 产品名称
        "订单金额合计": 28800.00,                   # ← B表第750行 订单金额
        "回款总额": 28800.00,                       # ← D表第76行 回款金额
        "回款率": "100.0%",                         # ← 计算字段
        "信用等级": "A",                            # ← A表第500行 信用等级
        "客户城市": "北京",                         # ← A表第500行 所在城市
        "最近订单日期": "2023-07-20",              # ← B表第750行 下单日期
    })

    # 其他结果行（模拟其他客户的汇总）
    for i in range(2, 51):
        cid = f"C{random.randint(1,4999):06d}"
        amount = round(random.uniform(5000, 100000), 2)
        payment = round(amount * random.uniform(0.5, 1.1), 2)
        rows.append({
            "客户ID": cid,
            "客户姓名": f"客户_{i}",
            "产品代码": f"P{random.randint(1,200):04d}",
            "产品名称": f"产品_{i}",
            "订单金额合计": amount,
            "回款总额": payment,
            "回款率": f"{payment/amount*100:.1f}%",
            "信用等级": random.choice(["A", "B", "C"]),
            "客户城市": random.choice(["北京", "上海", "广州"]),
            "最近订单日期": f"2023-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
        })

    df = pd.DataFrame(rows)
    path = OUTPUT_DIR / "R1.xlsx"
    df.to_excel(path, index=False)
    print(f"  ✓ {path} ({len(rows)}行)")
    print(f"\n  📌 R1第1行关键字段:")
    print(f"     客户ID=C000500 → A表第500行")
    print(f"     产品代码=P0042 → B表第750行 + C表第30行")
    print(f"     回款总额=28800 → D表第76行")


def main():
    print("=" * 60)
    print("🔍 数据链路侦探 — 生成演示数据")
    print("=" * 60)

    a_row = generate_A()
    b_row = generate_B(a_row["客户ID"])
    c_row = generate_C()
    d_row = generate_D()
    generate_rules()
    generate_R1(a_row, b_row, c_row, d_row)

    print("\n" + "=" * 60)
    print("✅ 演示数据生成完成！")
    print("\n设计链路：")
    print(f"  R1.xlsx 第1行")
    print(f"    ├── A.xlsx 第500行 (客户ID=C000500, 姓名=张伟华)")
    print(f"    ├── B.xlsx 第750行 (订单ORD00000750, 客户C000500, 产品P0042)")
    print(f"    ├── C.xlsx 第30行  (产品P0042=高端商务笔记本)")
    print(f"    ├── D.xlsx 第76行  (回款PAY000076, 订单ORD00000750)")
    print(f"    └── rules.md       (计算规则与定义)")
    print("\n现在可以运行演示：")
    print("  cd ..")
    print("  python scripts/trace.py \\")
    print("    --result examples/R1.xlsx --row 2 \\")
    print("    --sources examples/A.xlsx examples/B.xlsx examples/C.xlsx examples/D.xlsx examples/rules.md \\")
    print("    --out examples/chain.json --html examples/wall.html --verbose --summary")
    print()


if __name__ == "__main__":
    main()
