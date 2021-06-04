# efb-qq-plugin-go-cqhttp

efb-qq-plugin-go-cqhttp 是 efb-qq-slave 的插件，需要配合 efb-qq-slave 使用，使用前请先阅读 [efb-qq-slave 的文档](https://github.com/milkice233/efb-qq-slave/blob/master/README_zh-CN.rst)。

下面的教程展示了当 go-cqhttp 和 ehForwarderBot 在同一台机器上运行时如何设置两端。

（高级） 对于其他的情况，例如 go-cqhttp 和 ehForwarderBot 在不同的机器上运行时， `go-cqhttp port url` 和 `go-cqhttp api url` 必须修改为相应的值（前者是 efb-qq-slave 监听的地址/端口，后者是 go-cqhttp 监听的地址/端口），同时防火墙应允许双方的数据包通过，以便双方的请求不会被防火墙拦截。如果双方通信内容必须经过 Internet 传输，请确保已配置 `Access Token` 并启用 `HTTPS` 确保双方通信内容不会在公网被窃听/篡改。

有关 go-cqhttp 的详细信息，请访问 [go-cqhttp 文档](https://docs.go-cqhttp.org)。

## 配置 go-cqhttp

1. [下载 go-cqhttp](https://docs.go-cqhttp.org/guide/quick_start.html)
2. 编辑 `config.yaml` 配置文件，注意修改如下部分

   ```yaml
   account:         # 账号相关
     uin: 000000000 # QQ 账号
     password: ''   # QQ 密码，为空时使用扫码登录

   message:
     # 上报数据类型
     # efb-qq-plugin-go-cqhttp 仅支持 array 类型
     post-format: array

   # 默认中间件锚点
   default-middlewares: &default
     # 访问密钥，强烈推荐在公网的服务器设置
     access-token: ''

   servers:
     # HTTP 通信设置
     - http:
         # 是否关闭正向 HTTP 服务器
         disabled: false
         # 服务端监听地址
         host: 127.0.0.1
         # 服务端监听端口
         port: 5700
         # 反向 HTTP 超时时间, 单位秒
         # 最小值为 5，小于 5 将会忽略本项设置
         timeout: 5
         middlewares:
           <<: *default # 引用默认中间件
         # 反向 HTTP POST 地址列表
         post:
           - url: 'http://127.0.0.1:8000' # 地址
             secret: ''                   # 密钥保持为空
      ```
3. 运行 go-cqhttp `./go-cqhttp`

## 配置 efb-qq-slave 端

1. 安装 efb-qq-plugin-go-cqhttp `pip install git+https://github.com/XYenon/efb-qq-plugin-go-cqhttp`
2. 为 `milkice.qq` 从端创建 `config.yaml` 配置文件

   配置文件通常位于 `~/.ehforwarderbot/profiles/default/milkice.qq/config.yaml`

   样例配置文件如下:
   ```yaml
    Client: GoCQHttp                      # 指定要使用的 QQ 客户端（此处为 GoCQHttp）
    GoCQHttp:
        type: HTTP                        # 指定 efb-qq-plugin-go-cqhttp 与 GoCQHttp 通信的方式 现阶段仅支持 HTTP
        access_token:
        api_root: http://127.0.0.1:5700/  # GoCQHttp API接口地址/端口
        host: 127.0.0.1                   # efb-qq-slave 所监听的地址用于接收消息
        port: 8000                        # 同上
   ```

3. 启动 `ehforwarderbot`，大功告成！
