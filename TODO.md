# TODO

## Figma UI 验收：无 Dev Mode 接入

当前版本仅支持通过 Figma Desktop MCP 读取设计稿。由于 Desktop MCP 依赖 Dev Mode，没有相应权限时只能保留配置，无法实际读取设计数据。

后续计划：

- 优先支持 Figma REST API + Personal Access Token，读取指定节点的结构信息和渲染图片。
- REST API 方案仅申请设计文件读取权限，不依赖 Dev Mode。
- 没有 Figma Token 时，允许用户上传设计参考截图，继续执行视觉对比验收。
- 保留 Desktop MCP，作为具备 Dev Mode 环境时的可选接入方式。
- 前端分别展示“接入已配置”“服务可连接”“设计基线读取成功”三种状态，避免把已填写地址误认为服务可用。
- 对接口限流、Token 失效、文件无权限和节点不存在等问题提供可执行的中文错误提示。

状态：暂缓实现，保留现有第一版功能。
