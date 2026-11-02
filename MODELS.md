# 本地模型索引（不随仓库上传）

仓库里**不包含**语音模型文件：X-ASR 每一档约 586 MB，四档合计约 2.3 GB。
请按下面的索引自行下载，放到 `data/models/xasr/` 下即可；也可以设置
`XASR_MODEL_ROOT` 直接指向你已有的 `deployment/models`，完全不复制。

## 1. 下载源

X-ASR-zh-en（中英文流式/离线一体化 Zipformer transducer）：

- Hugging Face：<https://huggingface.co/GilgameshWind/X-ASR-zh-en>
- ModelScope：<https://www.modelscope.ai/Gilgamesh-J/X-ASR-zh-en>

下载其中 `deployment/models/` 下的四个目录（无需训练代码）：

```
deployment/models/
├── chunk-160ms-model/
├── chunk-480ms-model/
├── chunk-960ms-model/
└── chunk-1920ms-model/
```

## 2. 安装到项目

方式 A：复制到项目（推荐，最省心）

```text
data/models/xasr/
├── chunk-160ms-model/
├── chunk-480ms-model/
├── chunk-960ms-model/
└── chunk-1920ms-model/
```

方式 B：不复制，直接指向原目录（Windows 示例）

```powershell
$env:XASR_MODEL_ROOT = "D:\models\X-ASR-zh-en\deployment\models"
```

或在 `config.yaml` 里改 `asr.xasr_model_dir`。

## 3. 每个目录需要的文件

以 1920ms 为例（其余分块把文件名里的 `1920` 换成对应数字）：

| 文件 | 大小（约） |
|---|---|
| `tokens.txt` | 64 KB |
| `encoder-1920ms.onnx` | 566 MB |
| `decoder-1920ms.onnx` | 11 MB |
| `joiner-1920ms.onnx` | 10 MB |

四档目录结构相同，只是文件名中的毫秒数不同。**用哪一档由 `asr.xasr_chunk_ms` 决定**：

| `asr.xasr_chunk_ms` | 目录 | 特点 |
|---|---|---|
| `160` | `chunk-160ms-model` | 延迟最低，准确率最低 |
| `480` | `chunk-480ms-model` | 低延迟 |
| `960` | `chunk-960ms-model` | 平衡 |
| `1920` | `chunk-1920ms-model` | 默认，最准 |

只下载一档也能跑：把对应目录放好，再把 `asr.xasr_chunk_ms` 设成同一档即可。

## 4. 验证

```bash
python -m src.app check
```

体检里的「ASR 模型文件(X-ASR)」一项会显示实际加载的目录、分块大小和总大小。
也可以直接看 `python -m src.app run` 启动日志里的 `ASR 引擎：...` 一行。

## 5. 许可

X-ASR 模型与部署文件使用 Apache License 2.0，详见上游仓库的 `LICENSE`。
本项目只调用其 ONNX 推理接口，不修改模型文件。
