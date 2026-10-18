# AnyRouter 签到插件 (AnyRouterSign)

用于 **AnyRouter** (`https://anyrouter.top`) 站点的每日自动签到与额度监控。

## 配置说明
1. **站点地址**：默认 `https://anyrouter.top`
2. **站点 Cookie**：在 Edge 浏览器中登录 `https://anyrouter.top/console`，按 F12 打开开发者工具，切换到【网络】标签刷新页面，找到任意请求复制其【Cookie】请求头粘贴到此处。
3. **无头浏览器仿真**：默认开启，遇 Cloudflare/WAF 盾时自动调用无头浏览器仿真过盾。
4. **签到周期**：Cron 表达式，默认每天 08:00。
