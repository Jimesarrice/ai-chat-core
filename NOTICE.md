# 第三方组件与致谢

本项目以 MIT 许可发布（见 [LICENSE](LICENSE)）。以下项目/资源以各自许可证提供，
本仓库感谢原作者的工作：

## 运行时引擎（仅随 Releases 发布包分发，不提交进源码仓库）

| 项目 | 作者 | 许可 | 用途 |
|---|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | ggml-org | MIT | 本地大模型推理（Win-Vulkan 构建 b11829） |
| [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) | ServeurpersoCom | MIT | Qwen3-TTS 的 C++/GGML 实现（Vulkan 构建） |

## 功能设计参考（MIT）

本项目的心情/记忆/主动搭话/QQ 桥/朗读/联网搜集，其机制与提示词参考并重实现了
[Yuuyuko-uu](https://github.com/Yuuyuko-uu) 的以下 DSH 插件（MIT）：

- `dsh-fish-memory`（鱼的记忆）
- `dsh-feiyu-mood`（鱼的心情）
- `dsh-speak-up`（主动搭话）
- `dsh-qq-bridge`（通到QQ）
- `dsh-tts-bridge`（朗读桥）
- `dsh-bili-subagent`（刷B站）

`docs/reference-analysis/` 下的机制规格书包含对上述 MIT 源码的引用与逐字提示词摘录，
版权归原作者；本项目对其进行了独立重实现（Python / 独立应用形态）。

## 其他依赖

- FastAPI、uvicorn、httpx、pydantic：各自 MIT/BSD 许可
- [jieba](https://github.com/fxsjy/jieba)（MIT）：中文分词，词表源自其上游 `extra_dict`
- [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（MIT）：B站采集
- [Qwen3-TTS-GGUF](https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF) 模型（权重版权归 Qwen 团队，使用遵循其模型许可）

## 模型

Qwen3-VL / Qwen3-TTS 等模型权重版权归阿里云通义千问团队所有，
本仓库**不包含任何模型权重**；请从 ModelScope / HuggingFace 获取并遵守相应许可。
