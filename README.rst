客户端：go-cqhttp
====================================

由于 efb-qq-slave 的特性是由 go-cqhttp 客户端提供的，两者互相隔离，因此必须正确配置两端确保两端能够正常通信。

有关 go-cqhttp 的配置方法，请访问 `go-cqhttp 文档 <https://docs.go-cqhttp.org/>`_。

配置 ehForwarderBot 端篇
----------------------------------------------------

1. 为 ``milkice.qq`` 从端创建 ``config.yaml`` 配置文件

   *配置文件通常位于* ``~/.ehforwarderbot/profiles/default/milkice.qq/config.yaml``。

   样例配置文件如下:

   .. code:: yaml

       Client: GoCQHttp                      # 指定要使用的 QQ 客户端（此处为 GoCQHttp）
       GoCQHttp:
           type: HTTP                        # 指定 efb-qq-slave 与 GoCQHttp 通信的方式 现阶段仅支持HTTP
           access_token: ac0f790e1fb74ebcaf45da77a6f9de47
           api_root: http://127.0.0.1:5700/  # GoCQHttp API接口地址/端口
           host: 127.0.0.1                   # efb-qq-slave 所监听的地址用于接收消息
           port: 8000                        # 同上

2. 控制台启动 ``ehforwarderbot``, 大功告成!
