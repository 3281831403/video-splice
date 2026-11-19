# video-splice

## 题面

把分镜片段自动拼成成片后，接缝处总是卡一下：切点没有对齐 GOP，播放器只能从头解码而卡顿；拼接 PTS 直接累加原值，接缝处出现 33 ms 空洞；音频重采样按整段长度计算，片段一多累计漂移 20 ms；间隙检测只看相邻片段差值，跨越无效片段时漏检；插入转场只裁后一段，成片总时长反而变长；按 PTS 拆分时边界没有对齐最近帧，落在两帧之间；校验放过了零时长片段，编码器直接报错；越界的 in/out 不做钳制，负 PTS 进入处理管道；批量拼接中途失败会留下半成品列表；时间基换算使用浮点数，29.97 fps 下每 1000 帧偏 1 帧。edit/splice.py 是核心，tests/test_splice.py 15个红。要修的是：…

## 运行

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始状态下应恰好得到 `15 failed`，不应出现 collection、import 或 syntax error。测试只依赖 Python 标准库和 pytest，离线即可运行，不使用随机数，也不 sleep。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 约束

`tests/test_splice.py` 是验收契约，不可修改；实现只改 `edit/splice.py`。片段使用内联字典 `{"in": int, "out": int, "gop": int, "keyframes": [...]}`，PTS 单位为毫秒，时间基使用整数或有理数对，所有边界容差为 1 ms。
