## 这个 PR 做了什么

<!-- 一句话说清。如果是修 bug，写清楚「原来的行为 → 现在的行为」。 -->

## 为什么这么做

<!-- 取舍是什么。有没有更简单的做法被排除了，为什么。 -->

## 自检

- [ ] 全量测试绿：`python -m unittest discover -s tests`
- [ ] 改了 shell 脚本的话跑过 `bash -n install.sh`
- [ ] 改了版本号的话，`pyproject.toml` / `install.sh` / `install.ps1` / `package.json` 四处一致
- [ ] 加了 MCP 工具的话，带上了注解，并且文档里的工具数对得上（`test_doc_truth` 会查）
- [ ] 没有提交任何凭据、cookie、个人 store 内容
- [ ] 新增的宿主接入（如果有）在 `tests/test_connect.py` 里有探测 / 写入 / 幂等三条用例

## 影响面

<!-- 会不会影响已经在用的部署？需不需要迁移？要不要改 store 里的文件？ -->

- [ ] 不需要迁移，老 store 直接可用
- [ ] 需要迁移（说明：）
