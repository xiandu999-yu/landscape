---
name: BUG 报告模板 / Bug Report Template
about: 提交可复现的缺陷报告 / Report reproducible defects
title: "[BUG] "
labels: bug
assignees: ''

---

### 核心问题摘要 / Core Issue
<!-- 用一句话说明问题本质 -->
<!-- One-sentence description of the core problem -->

### 环境信息 / Environment
<!-- 必须提供 -->
<!-- Required information -->
- 操作系统 / OS: <!-- e.g. Windows 11 22H2 -->
- 软件版本 / Version: <!-- e.g. v2.3.1 或 commit hash -->
- 运行环境 / Runtime: <!-- Node.js 18.17.0 / Python 3.11 -->
- 设备型号 / Device: <!-- iPhone 15 Pro / ThinkPad X1 Carbon -->

### 复现路径 / Reproduction Path
1. **必要条件** / Prerequisites：
   - 需要配置的特殊参数 / Special config: 
   - 依赖的特定数据 / Required data: 

2. **准确复现步骤** / Exact Steps：
   - 步骤1 / Step 1: <!-- 点击登录按钮 -->
   - 步骤2 / Step 2: <!-- 输入错误密码 -->
   - 步骤3 / Step 3: <!-- 观察响应 -->

3. **出现频率** / Frequency：
   - [ ] 每次必现 / Always
   - [ ] 间歇出现 / Intermittent 
   - [ ] 仅出现一次 / Once

### 预期与实际结果 / Expected vs Actual
```diff
+ 预期结果 / Expected:
- 应该显示密码错误提示
- Should show password error message

- 实际结果 / Actual:
+ 应用程序直接崩溃退出
+ Application crashes immediately
