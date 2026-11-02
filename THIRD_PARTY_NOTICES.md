# 第三方组件与许可

本项目在运行/分发时涉及以下第三方组件。各组件的版权与许可归其作者所有。

## blivedm

- 位置：`blivedm/`
- 用途：B 站直播弹幕 WebSocket 客户端与协议模型
- 许可证：MIT
- 上游：<https://github.com/xfgryujk/blivedm>
- 版权：Copyright (c) 2018 xfgryujk
- 许可证全文：`blivedm/LICENSE`
- 说明：本仓库内为较早快照并含本地适配修改；MIT 允许修改与再分发，但需保留上述版权与许可声明。

## X-ASR（语音识别模型）

- 用途：中英文流式 Zipformer transducer ASR（ONNX）
- 许可证：Apache License 2.0
- 模型：<https://huggingface.co/GilgameshWind/X-ASR-zh-en>
- 镜像：<https://www.modelscope.ai/Gilgamesh-J/X-ASR-zh-en>
- 说明：模型文件不随本仓库分发，下载方式见 [MODELS.md](MODELS.md)。

## sherpa-onnx

- 用途：ONNX 语音识别推理运行时（Python 包 `sherpa-onnx`）
- 许可证：Apache License 2.0
- 上游：<https://github.com/k2-fsa/sherpa-onnx>

## 其他 Python 依赖

`aiohttp`、`PyYAML`、`numpy`、`zhconv`、`qrcode`、`Pillow`、`psutil`、`brotli` 等，
各自遵循其 PyPI 页面上声明的许可证。

---

## 本项目自身

本项目代码使用 MIT 许可证，Copyright (c) 2026 AnoneX2，全文见根目录 [LICENSE](LICENSE)。
第三方组件仍遵循各自许可证；MIT 许可不改变 blivedm、X-ASR、sherpa-onnx 的授权条件。
