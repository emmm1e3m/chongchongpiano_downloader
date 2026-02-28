# 这是一个下载虫虫钢琴网vip乐谱的python程序

包括两个版本：包体较小但需要手动更新驱动的edgedriver版本 和 包体较大但是下载即用的playwright版本
- edgedriver版本：由edge_webdriver驱动，需确保edge已在电脑上安装，并确保edge_webdriver与edge版本对应，且edge_webdriver与下载器在同一目录下（你需要下载两个文件）。若出现错误可以考虑更新edge_webdriver驱动：前往[这里](https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/?form=MA13LH#downloads)下载对应版本（对于大多数电脑选择stable_x86）的msedgedriver.exe，解压后将替换此文件夹内同名exe文件
- playwright版本（推荐使用）：内置了独立的chromium，由playwright驱动

---

- 输入的链接形如```https://www.gangqinpu.com/cchtml/1121785.htm```，是PC版网页而非手机版

- 输入空行指在最后一个链接粘贴完后连敲两次enter键
  
- [原理参考](https://www.52pojie.cn/thread-1470976-1-1.html)，本项目仅对其进行封装

- 下载的乐谱并未移除水印以避免学习之外的用途
