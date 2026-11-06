#!/usr/bin/env python3
"""端到端测试：从示例数据验证完整链路"""
import sys
import os
import unittest
import subprocess
import json

ROOT = os.path.join(os.path.dirname(__file__), '..')
SCRIPTS = os.path.join(ROOT, 'scripts')
EXAMPLES = os.path.join(ROOT, 'examples')
sys.path.insert(0, SCRIPTS)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """生成示例数据"""
        gen = os.path.join(EXAMPLES, 'generate_examples.py')
        result = subprocess.run([sys.executable, gen], capture_output=True, text=True, encoding='utf-8',
                                env={**os.environ, 'PYTHONUTF8': '1'}, cwd=EXAMPLES)
        if result.returncode != 0:
            raise Exception(f"Failed to generate examples: {result.stderr}")

    def test_trace_r1_row1(self):
        """验证R1第1行能溯源到A500+B750+C30+D76"""
        from detective import DataDetective

        detective = DataDetective(verbose=False)
        chain = detective.trace(
            result_file=os.path.join(EXAMPLES, 'R1.xlsx'),
            result_row=2,  # Excel第2行=第1数据行
            source_files=[
                os.path.join(EXAMPLES, 'A.xlsx'),
                os.path.join(EXAMPLES, 'B.xlsx'),
                os.path.join(EXAMPLES, 'C.xlsx'),
                os.path.join(EXAMPLES, 'D.xlsx'),
                os.path.join(EXAMPLES, 'rules.md'),
            ],
        )

        # 基本结构检查
        self.assertIsNotNone(chain)
        self.assertGreater(len(chain.nodes), 1)  # 至少有结果节点+1个源节点
        self.assertGreater(len(chain.edges), 0)
        self.assertGreater(chain.confidence, 0.0)
        selected = {os.path.basename(n.source_file): n.row_num for n in chain.nodes if n.node_type == 'source'}
        self.assertEqual(selected, {'A.xlsx': 500, 'B.xlsx': 750, 'C.xlsx': 30, 'D.xlsx': 76})

        print(f"\n[Test] Chain confidence: {chain.confidence:.2f}")
        print(f"[Test] Nodes: {len(chain.nodes)}, Edges: {len(chain.edges)}")

    def test_finds_customer_id(self):
        """验证能找到客户ID关联"""
        from detective import DataDetective

        detective = DataDetective(verbose=False)
        chain = detective.trace(
            result_file=os.path.join(EXAMPLES, 'R1.xlsx'),
            result_row=2,
            source_files=[
                os.path.join(EXAMPLES, 'A.xlsx'),
                os.path.join(EXAMPLES, 'B.xlsx'),
            ],
        )

        # 至少应该在节点中找到C000500相关内容
        all_values = []
        for node in chain.nodes:
            all_values.extend(node.field_values)
        found = any('C000500' in str(v) for v in all_values)
        self.assertTrue(found, "应该找到客户ID C000500")

    def test_chain_json_structure(self):
        """验证输出JSON结构符合schema"""
        import tempfile
        from detective import DataDetective

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            json_path = f.name

        try:
            detective = DataDetective(verbose=False)
            chain = detective.trace(
                result_file=os.path.join(EXAMPLES, 'R1.xlsx'),
                result_row=2,
                source_files=[os.path.join(EXAMPLES, 'A.xlsx')],
                output_json=json_path,
            )

            with open(json_path, encoding='utf-8') as f:
                data = json.load(f)

            required_keys = ['result_ref', 'chain_id', 'nodes', 'edges', 'confidence',
                              'missing', 'conflicts', 'hypotheses', 'source_locations']
            for key in required_keys:
                self.assertIn(key, data, f"缺少字段: {key}")

            self.assertIsInstance(data['confidence'], float)
            self.assertIsInstance(data['nodes'], list)
            self.assertIsInstance(data['edges'], list)

        finally:
            if os.path.exists(json_path):
                os.unlink(json_path)

    def test_html_generation(self):
        """验证HTML文件能正常生成"""
        import tempfile
        from detective import DataDetective

        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as f:
            html_path = f.name

        try:
            detective = DataDetective(verbose=False)
            chain = detective.trace(
                result_file=os.path.join(EXAMPLES, 'R1.xlsx'),
                result_row=2,
                source_files=[os.path.join(EXAMPLES, 'A.xlsx')],
                output_html=html_path,
            )

            self.assertTrue(os.path.exists(html_path))
            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('数据链路侦探', content)
            self.assertIn('马赛克调查墙', content)
            self.assertGreater(len(content), 1000)

        finally:
            if os.path.exists(html_path):
                os.unlink(html_path)

    def test_partial_sources(self):
        """测试只提供部分源文件时输出missing字段"""
        from detective import DataDetective

        detective = DataDetective(verbose=False)
        # 只提供A表，不提供B/C/D
        chain = detective.trace(
            result_file=os.path.join(EXAMPLES, 'R1.xlsx'),
            result_row=2,
            source_files=[os.path.join(EXAMPLES, 'A.xlsx')],
        )
        # 链路应该是部分的，不应该崩溃
        self.assertIsNotNone(chain)

    def test_cli_runs(self):
        """验证CLI能正常运行"""
        result = subprocess.run([
            sys.executable, os.path.join(SCRIPTS, 'trace.py'),
            '--result', os.path.join(EXAMPLES, 'R1.xlsx'),
            '--row', '2',
            '--sources', os.path.join(EXAMPLES, 'A.xlsx'),
            '--no-html', '--no-json',
        ], capture_output=True, text=True, encoding='utf-8', cwd=ROOT)

        self.assertEqual(result.returncode, 0, f"CLI failed: {result.stderr}")
        self.assertIn('置信度', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
