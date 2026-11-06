# 合成案件盲测

`cases/public.json` 是发给被测模型的原始材料。`private/rubrics.json` 只供本地评分，绝不拼入模型消息。`response_contract.md` 是所有案件共用的答卷协议。案件没有预设必须猜中的作案者；检验重点是证据边界、时间校正、转述依赖、候选区分，以及下一步调查是否有明确对象和鉴别结果。

此 runner 是 **一次文本调用的受限调查辅助 Agent 评估**：它将实际 `SKILL.md` 和该模式的参考资料作为说明，输入公开案件，不给被测模型工具、文件系统或评分规范。它不执行 skill 的 CLI，不等同于多轮自主取证 Agent。若换成能访问整个仓库的 Agent，`private/` 的命名不构成隔离，必须把公开材料复制到独立的只读沙箱。

```powershell
# 默认仅列案件，不读取密钥、不联网
python test/run_blind_eval.py

# 复跑已保存答卷（每个案件对应 C01.json 等）
python test/run_blind_eval.py --responses-dir <答卷目录> --out test/results/offline.json

# 明确允许调用已配置的大模型；兼容 API_KEY / BASE_URL / MODEL
python test/run_blind_eval.py --live --env-file tests/.env --out test/results/live.json

# 缩小调用规模；重试也扣减总请求预算
python test/run_blind_eval.py --live --case C01 --case C02 --max-requests 2 --max-tokens 5000 --timeout 90

# 服务中原配置模型不可用时，显式选择本次使用的模型，不改.env
python test/run_blind_eval.py --live --model <服务提供的模型标识> --out test/results/alternate-model.json

# 服务支持推理预算参数时，可显式降低推理消耗以留出最终答卷空间
python test/run_blind_eval.py --live --model <模型标识> --reasoning-effort low --max-tokens 8000 --timeout 300 --out test/results/low-effort.json

# 不联网的机制回归
python -m unittest discover -s tests -p test_blind_eval.py -v
```

默认从 `test/.env` 读取配置，该文件不存在时兼容已有的 `tests/.env`；`--env-file` 可显式指定。默认最多8次HTTP请求、每次最多5000输出token、90秒超时、无自动重试。`--retries 1` 只重试连接错误或HTTP 408/429/5xx，最多可设2；总HTTP次数受 `--max-requests` 约束。费用取决于配置的服务，报告的token是服务返回的用量，不是货币成本。认证只放请求头，禁止跟随重定向，错误报告不保存地址、认证头或服务端错误正文。模型文本并非现实证据。

模型响应较慢时可显式设置 `--timeout 300`（范围1至300秒，默认90秒）。每次发起HTTP请求前、每案完成后都会原子写入 `--out` 检查点；运行中可读取其中的已完成答卷、累计已知用量和 `pending_case`。进程被中断后检查点保留最后完成的案件，待完成请求的费用标为未知，不应视为零。`run_status=completed` 才表示所选案件的循环结束；这不保证所有案件通过。检查点不自动续跑或重发已完成请求，后续可用 `--case` 显式选择未完成案件并写入另一份报告。

`finish_reason=length` 即使返回文本恰好是合法JSON，也作为截断错误记录；报告保留模型原始 `answer_text`、返回的token用量和延迟。未截断但不是合法JSON的模型content同样保留供诊断。服务端HTTP错误正文仍不会写入报告。

请求不发送 `temperature`、`response_format` 或 `tools`，减少推理模型与兼容API不支持可选参数的问题；使用文本协议要求JSON。默认上限参数名为 `max_tokens`；服务要求另一种命名时，使用 `--token-parameter max_completion_tokens`。HTTP400不自动切换参数重试，以便明确控制请求量和费用。无论选择哪种命名，总保留输出token上限；推理token是否包含在上限内由服务定义。

`--reasoning-effort` 是可选参数，默认不发送；可选 `low / medium / high / xhigh / max`，是否接受由模型服务决定，报告记录实际请求值。某些推理模型可能将输出上限全部用于 `reasoning_content`，最终 `content` 缺失。runner先保留用量和结束原因，再判断是否有最终答卷；仅记录推理字符数及服务报告的推理token数，不保存推理正文、不把推理正文当答卷。盲测提示最后明确以 `response_contract.md` 为唯一输出格式，替代技能常规 `analysis.json`，避免同时输出两套报告。

报告保留本次请求的模型标识、每案服务返回的模型标识（服务有提供时）、原始答卷、技能参考SHA256、提示词SHA256、缺失参考列表、延迟、token用量及逐项通过/失败诊断。`contract_pass` 只说明机器可检查的结构、证据ID、固定算术和候选约束通过，**不是案件推理准确率**。即使全部检查通过，仍需独立复核假说是否真正竞争、引文是否支持结论、行动是否可行、优先级是否合理、正文有没有与结构化字段矛盾的断言。报告明确标记 `semantic_review_required`，不使用被测模型给自己打分。

该小样本没有真实案件外部验证、模型基线或重复采样。可将它用于发现退化和建立后续评估，不应据此宣称现实办案能力已经可靠。调试时避免把 `tests/.env`、请求头或未脱敏配置加入报告和发布包。

## 推理模型的输出预算

`--max-tokens` 可能同时覆盖推理与最终答卷。某些模型在默认深度下会先耗尽预算，返回 `finish_reason=length` 且只有 `reasoning_content`；这属于截断，不能评为案件推理失败，也不能把推理正文当最终答卷。runner保留截断状态、用量及推理字符数，不保存推理正文。

需要时显式指定推理档位并预留足够最终输出空间，例如：

```powershell
python test/run_blind_eval.py --live --model MiniMax-M3.1-Flash --reasoning-effort low --max-tokens 8000 --timeout 300 --out test/results/live-minimax-low.json
```

`--model` 仅覆盖本次调用，不修改 `.env`。先核对服务实际可用的模型标识；配置中的不存在别名会导致404。`--reasoning-effort` 默认不发送，兼容不支持该字段的接口。MiniMax这类模型的档位和思考/最终字段区别见 [官方调用文档](https://platform.minimax.io/docs/guides/text-generation)。比较不同运行时必须同时报告模型、档位、token上限和提示版本，不能把改变推理档位的差异归因于skill本身。

每次请求开始、每案结束均原子写检查点；中断时已完成答卷仍保留，进行中请求的费用/用量标为未知。仅保存服务实际返回的用量，缺失用量不能算零。
