# 📄 论文查重工具(Paper Plagiarism Check)

一个**纯本地运行**的论文查重 + AIGC 疑似度分析工具,基于 Python 标准库实现(零第三方依赖),提供拖拽式网页界面。

![AIGC 分析](docs/screenshot-aigc.png)

## ✨ 三种模式

| 模式 | 说明 | 是否需要联网 |
|---|---|---|
| 🌐 在线检索查重 | 只需拖入论文。自动切句后逐句到开放学术库与搜索引擎检索原句出处,命中后抓取原文**逐字核验** | ✅ |
| 📁 与本地文献比对 | 拖入论文 + 参考文献/往稿/文件夹,基于 k 连续词元窗口做本地比对 | ❌ |
| 🔍 AIGC 疑似分析 | 离线启发式统计,估计"AI 生成风格疑似度"(0-100),逐句彩色标注 | ❌ |

![在线检索](docs/screenshot-online.png)

## 🚀 快速开始

**环境要求**:Python 3.8+(仅标准库;.pdf 支持需额外 `pip install pypdf`)。没有 Python 的话,先去 [python.org](https://www.python.org/downloads/) 安装。

```bash
# 可选:如需读取 PDF 文献,安装 pypdf(.docx/.txt/.md 无需任何依赖)
pip install pypdf

# 启动网页版(自动打开浏览器,默认 http://127.0.0.1:8765)
python scripts/webapp.py
```

Windows 用户可直接双击 [`start_web.bat`](start_web.bat)。

命令行方式:

```bash
python scripts/check_plagiarism.py --paper 论文.docx --refs 文献目录/ 旧稿.txt \
    --out 查重报告.md --html 查重报告.html --json 查重结果.json
```

### 安装为 ZCode 技能

1. 点本页 **Code → Download ZIP**(或 `git clone` 本仓库)
2. 解压。⚠️ **注意**:Download ZIP 解压出来的文件夹名是 `paper-plagiarism-check-main`,**必须重命名为 `paper-plagiarism-check`**(技能名与文件夹名一致才能被发现;git clone 的无需改名)
3. 把文件夹放入以下任一位置:
   - 个人级:`~/.agents/skills/paper-plagiarism-check/`(Windows 即 `C:\Users\你的用户名\.agents\skills\`)
   - 项目级:`<你的项目>/.zcode/skills/paper-plagiarism-check/`
4. **新开一个 ZCode 会话**,直接说"帮我给这篇论文查重"即可自动触发

## 🔍 检测原理

- **本地比对**:中文逐字、英文按词切分为"词元"序列,归一化(忽略大小写/全半角/空白/标点)后,凡与参考文献存在 k 个连续相同词元(默认中文 13、英文 6)即计为重复,合并为最大片段。这是常见"连续 13 字判重"思路的本地实现。
- **在线检索**:论文切句 → [OpenAlex](https://openalex.org)(约 2.5 亿篇文献题录摘要)/ [Europe PMC](https://europepmc.org)(开放获取全文)/ 搜索引擎(360/搜狗/Bing)检索候选 → 抓取原文逐字核验,只把核验通过的计入重复率。
- **AIGC 疑似度**:句长均匀度(burstiness)、模板短语密度、连接词开头句占比、模糊限定词密度、列举密度五项信号的加权启发式评分。

## ⚠️ 如实说明(重要)

- 开放数据源**无法访问知网/万方/维普等商业库的授权全文**;网页检索受搜索引擎排序限制,是尽力检索。结果为参考指标,**不能等同于知网 / Turnitin 的重复率**。
- 逐字重复才能命中,同义改写、语序调整、中英互译后的雷同无法检出。
- AIGC 疑似度是启发式统计,不构成 AI 写作的证明:人写的模板化文章也会得分,AI 写的朴素句子也可能低分,商用检测系统同样存在误判。
- 全程本地运行,默认只监听 127.0.0.1;在线模式只会把"句子片段"发送给公开检索数据源,不会上传整篇文档。

## 📂 目录结构

```
├── README.md
├── SKILL.md                 # ZCode 技能定义
├── start_web.bat            # Windows 一键启动
├── scripts/
│   ├── webapp.py            # 网页版(纯标准库 HTTP 服务)
│   ├── check_plagiarism.py  # 检测核心 + 命令行入口
│   ├── online_check.py      # 在线多源检索
│   └── aigc_check.py        # AIGC 启发式分析
└── docs/                    # 截图
```

## License

[MIT](LICENSE)
