# 传输运行时

本指南对应当前 `main` 源码；安装方式见
[README](../README_zh.md#安装当前源码)。

## 生命周期

经典 CAN 与 CAN FD 使用相同的显式生命周期：

```python
from linkerbot_transport.can import CANTransport

transport = CANTransport("can0")
try:
    transport.open()
    health = transport.get_health()
finally:
    transport.close()
```

构造阶段不打开硬件或启动 worker。`open()`、`close()` 幂等；清理失败会保留在对象上，
可通过 `close()` 或 `recover()` 重试。恢复会生成新的连接代次、关闭旧路由且不重放旧命令，
恢复后需要重新注册路由。

多个 SDK 设备共用物理通道时使用 `SharedTransportPool`。第一个租约打开 Transport，
最后一个租约关闭；清理失败时对象仍保留在池中。已释放的租约可再次 `close()` 重试空闲
清理，不重新打开通道或重复扣减引用，其 `transport` 属性仍不可用。
后续相同 key 的 `acquire()` 可以恢复该对象；旧租约重试不会关闭已恢复的活跃连接或替换项。

`acquire()` 抛出异常而没有返回租约时，调用 `pool.close_idle(key)` 重试该 key 的清理。
它与 `acquire()` 使用同一把锁，仅关闭池中没有活动租约的残留连接，不重新打开通道；
key 不存在或仍有活动租约时不执行关闭。关闭异常向调用方传播，条目保留供再次清理或恢复。

## 精确接收路由

路由是独占、有界的订阅。每个帧只按一个 key 投递到匹配路由，不向所有订阅者广播。
经典 CAN 的 key 包含完整仲裁 ID 和标准帧/扩展帧标记：

```python
import can

from linkerbot_transport.can import CANRouteKey, CANTransport

transport = CANTransport("can0")
try:
    transport.open()
    with transport.subscribe_route(
        CANRouteKey(arbitration_id=0x28, is_extended_id=False),
        queue_capacity=64,
    ) as route:
        receipt = transport.send(
            can.Message(arbitration_id=0x28, data=[0xC2], is_extended_id=False)
        )
        receipt.result(timeout=0.1)
        message = route.receive(timeout_s=0.02)
finally:
    transport.close()
```

响应分布在多个仲裁 ID 时，使用 `subscribe_routes()` 原子注册一组 key。整组共享一个
有界队列并保持到达顺序；任意 key 无法取得时整组都不注册。

协议地址编码在 CAN ID 中变化时，`CANTransport` 可接收 `route_key_of`。提取器接收原始
`can.Message`，必须返回 `CANRouteKey`，只做快速计算且不能 I/O。提取异常会计数并丢弃该帧，
投递的消息仍保留原始 ID、标记和数据。

SDK actor 可在订阅时传入 `on_available=event.set`。回调在接收路径同步执行，必须立即返回，
不能执行协议 I/O；路由关闭或中止时也会通知。`RoutedMessage` 包含单调接收时间、连接代次
和代次内序号。队列满时丢弃新帧，路由未命中、提取异常、溢出、关闭丢帧和代次拒绝都记录在
健康状态中。

发送回执和结果新增可选 `claimed_order`，路由消息新增可选 `received_order`。
这些进程内事件序号不依赖时钟精度，即使接收和 claim 的时间戳相同，也只有
`received_order > claimed_order` 的帧才发生在 claim 之后。它们不是时间戳、线上事务标识，
也不能跨进程比较；仍需检查连接代次和协议响应路由。

原有元数据构造方式保持有效，新字段默认 `None`。旧版或第三方传输未提供事件序号时，
调用方仍回退到时间戳判断，该兼容路径不能区分相同时间戳的事件。

## 健康状态与连接代次

`get_health()` 只读取缓存，返回连接状态、代次、最近/连续错误、重连次数、路由计数，以及
物理通道累计的 `tx_frames`、`rx_frames`、`tx_bytes` 和 `rx_bytes`。

发送计数在后端接收发送帧后增加；接收计数在后端交出帧时增加，即使随后路由丢弃。字节数按
DLC 选择的数据字段长度计算，不包含仲裁、CRC、位填充和帧间隔，不能直接当作总线利用率。
关闭、中止或恢复会关闭路由并唤醒阻塞接收方；旧代次到达的帧只计数，不投递到新路由。

## 发送回执

`transport.send()` 返回 `SendReceipt`。完成表示物理后端已接收帧，不表示设备已执行：

```python
from linkerbot_transport.errors import TimeoutError, TransportError

receipt = transport.send(message)
try:
    result = receipt.result(timeout=0.1)
except TimeoutError:
    # 有界等待结束，回执之后仍可能完成。
    ...
except TransportError:
    # 后端拒绝或发送失败。
    ...
```

成功结果为 `SendOutcome.DISPATCHED`，带连接代次和时间戳；被替换的 latest 目标为
`SendOutcome.SUPERSEDED`；物理失败为 `SendOutcome.FAILED` 并抛出稳定传输异常。停止
Transport 会使未完成回执失败，控制帧不会自动重试或重放。

## TransmitArbiter

多个设备或命令族共用 Transport 时使用 `TransmitArbiter` 作为单写入者。连续控制命令按
`(endpoint_id, command_family)` 各使用一个 latest 槽位，离散操作使用有界 FIFO：

```python
import time

from linkerbot_transport import TransmitArbiter

generation = transport.get_health().generation
arbiter = TransmitArbiter(transport.send, fifo_capacity=32)
try:
    arbiter.open(generation=generation)
    receipt = arbiter.submit_latest(
        frame,
        endpoint_id="left-hand",
        command_family="angles",
        deadline_at_s=time.monotonic() + 0.02,
    )
    arbiter.submit_fifo(clear_fault_frame, endpoint_id="left-hand")
finally:
    arbiter.close()
```

`deadline_at_s` 是绝对单调时钟截止时间。`replace_latest_if_pending()` 只替换尚未领取的
目标，目标已在途时返回 `None`。arbiter 健康状态为零 I/O 快照，包含 pending、accepted、
dispatched、superseded、failed 和容量拒绝计数。

已有 I/O actor 时使用 `TransmitMode.CALLER`，由该 actor 调用 `drain()`：

```python
from linkerbot_transport import TransmitArbiter, TransmitMode

arbiter = TransmitArbiter(transport.send, mode=TransmitMode.CALLER)
try:
    arbiter.open(generation=generation)
    receipt = arbiter.submit_fifo(frame, endpoint_id="left-hand")
    arbiter.drain()
    receipt.result(timeout=0.1)
finally:
    arbiter.close()
```

需要整组准入时先编码全部帧，再调用 `submit_fifo_batch()`。容量按帧计算，准入全有或全无，
组内不会插入其他任务。`max_pending_age_s` 在最老的排队或在途任务达到年龄上限时拒绝新
批次；`deadline_at_s` 仅在发送第一帧前检查，不中断已开始的批次。
物理 I/O 中止时通过 `BatchSendReceipt.sent_frames` 查看已发送前缀。准入原子不等于物理执行
或设备确认原子。

## CAN FD 帧值

`CANFDMessage.frame_type` 默认 `0x04`（FD、不启用 BRS）；设备支持时使用 `0x0C`。`None`
继承后端模式，SocketCAN 保持 BRS 关闭。`dlc` 使用协议编码 `0..15`，后端负责转换为数据
字段长度并补零；经典 CAN 最多携带 8 个数据字节。
